import { expect, test } from '@playwright/test';
import { signIn, TEST_SESSION } from './session.js';

// The dashboard's supervised single-drone hover control (#171). The GCS does all the gating; these tests check
// what the operator sees and what reaches the backend: nothing is sent on the first click, one request on the
// confirming click, the GCS's own refusal text is shown as it is, and the control is not offered where it must not
// be (hardware mode, a role below FLIGHT_COMMANDER, no drone connected, or before the GCS has reported its mode).
// The backend is stubbed; telemetry arrives over a mocked WebSocket in the GCS's wire format.

const FAST_FIELDS = [
  'fleet_x', 'fleet_y', 'fleet_z', 'local_x', 'local_y', 'local_z', 'heading',
  'armed', 'connected', 'planned_x', 'planned_y', 'planned_z', 'planned_valid',
  'led_r', 'led_g', 'led_b', 'led_valid',
];
const FRAME_HEADER = [7243, 1, FAST_FIELDS.length];

function fastFrame(drones) {
  const buf = Buffer.alloc((FRAME_HEADER.length + drones.length * FAST_FIELDS.length) * 4);
  FRAME_HEADER.forEach((w, k) => buf.writeFloatLE(w, k * 4));
  drones.forEach((d, i) => FAST_FIELDS.forEach((field, j) => {
    const v = typeof d[field] === 'boolean' ? Number(d[field]) : (d[field] ?? 0);
    buf.writeFloatLE(v, (FRAME_HEADER.length + i * FAST_FIELDS.length + j) * 4);
  }));
  return buf;
}
const slowFrame = (drones) => JSON.stringify({
  type: 'slow',
  drones: Object.fromEntries(drones.map((d, i) => [String(i), {
    flight_mode: 'HOLD', battery_percent: 95, battery_voltage: 16.4, gps_lock: true, satellites: 14,
    gps_fix_type: 'FIX_3D', comm_latency_ms: 12, home_lat: 47.397, home_lon: 8.545, home_alt: 488, port: 14540 + i,
  }])),
});

const MODE = {
  mode: 'SIMULATION', backend: 'px4', fidelity: 'px4-sitl', hardware_profile: null, hardware_flight_certified: false,
  telemetry_freshness_s: 0.1, telemetry_status: 'FRESH', rc_takeover_available: false,
};

const COMPLETED = {
  status: 'COMPLETED', mode: 'SIMULATION', active_backend: 'px4',
  steps: [
    { step: 'PREFLIGHT', status: 'PASSED', drone_id: 0 },
    { step: 'ARM', status: 'ARMED' },
    { step: 'HOVER_REACHED', status: 'HOLDING', altitude_m: 0.86 },
    { step: 'HOVER_HOLD_COMPLETE', status: 'STABLE', dwell_s: 5.04 },
    { step: 'LANDING_COMMANDED', status: 'LANDING' },
    { step: 'TOUCHDOWN_DISARM', status: 'DISARMED' },
  ],
};

/** Opens the dashboard; returns the hover requests the backend received. */
async function open(page, { drones = [{ connected: true }], mode = MODE, session = TEST_SESSION, modeDelay = null, hover = { status: 200, json: COMPLETED } } = {}) {
  const hoverCalls = [];
  await page.routeWebSocket('**/ws/telemetry', (ws) => { ws.send(fastFrame(drones)); ws.send(slowFrame(drones)); });
  await page.route('**/api/mode', async (route) => {
    if (modeDelay) await modeDelay;
    return route.fulfill({ json: mode });
  });
  await page.route('**/api/sim/status', (route) => route.fulfill({ json: {} }));
  await page.route('**/api/single-drone/hover-sequence', async (route) => {
    hoverCalls.push(route.request().postDataJSON());
    if (typeof hover === 'function') return hover(route);
    return route.fulfill(hover);
  });
  await signIn(page, session);
  await page.goto('/');
  return hoverCalls;
}

const button = (page) => page.getByRole('button', { name: /HOVER TEST|CONFIRM: ARM AND CLIMB|HOVER SEQUENCE RUNNING/ });

// Arm, wait out ConfirmButton's guard against a reflexive double-click (350 ms), then confirm.
async function armAndConfirm(page) {
  await button(page).click();
  await expect(button(page)).toHaveAttribute('data-armed', 'true');
  await page.waitForTimeout(450);
  await button(page).click();
}

test('a single click does not start the hover; the confirming click sends one request and shows the steps', async ({ page }) => {
  const calls = await open(page);
  await expect(button(page)).toBeEnabled();

  await button(page).click();
  await page.waitForTimeout(400);
  expect(calls).toHaveLength(0);
  await expect(button(page)).toHaveAttribute('data-armed', 'true');
  await expect(button(page)).toContainText('CONFIRM');

  await button(page).click();
  await expect(page.getByTestId('hover-test-result')).toContainText('COMPLETED');
  expect(calls).toEqual([{ target_altitude_m: 1.0, hover_duration_s: 5.0 }]);
  const result = page.getByTestId('hover-test-result');
  await expect(result).toContainText('Pre-arm gates: PASSED');
  await expect(result).toContainText('Hover reached: HOLDING (0.86 m)');
  await expect(result).toContainText('Hover held: STABLE (5.0 s)');
  await expect(result).toContainText('Touchdown and disarm: DISARMED');
  await expect(button(page)).toHaveAttribute('data-armed', 'false');
});

test('a pre-arm refusal shows the GCS reasons, not [object Object]', async ({ page }) => {
  await open(page, {
    hover: {
      status: 412,
      json: { detail: { message: 'Single-drone sequence refused: pre-arm gates are red.', profile: 'SIMULATION', blocking_reasons: ['Battery state of charge — 0.0%', 'EKF health & home lock — no home'] } },
    },
  });
  await armAndConfirm(page);
  const error = page.getByTestId('hover-test-error');
  await expect(error).toContainText('pre-arm gates are red.');
  await expect(error).toContainText('Battery state of charge — 0.0%; EKF health & home lock — no home');
  await expect(error).not.toContainText('[object');
});

test('a failed sequence shows the GCS text and is not reported as completed', async ({ page }) => {
  await open(page, { hover: { status: 500, json: { detail: 'Hover dwell failed: Drone 0 disconnected during hover dwell' } } });
  await armAndConfirm(page);
  await expect(page.getByTestId('hover-test-error')).toContainText('Hover dwell failed: Drone 0 disconnected during hover dwell');
  await expect(page.getByTestId('hover-test-result')).toHaveCount(0);
});

test('while a sequence runs the control is locked and no second request can be sent', async ({ page }) => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  const calls = await open(page, { hover: async (route) => { await held; return route.fulfill({ json: COMPLETED }); } });

  await armAndConfirm(page);
  await expect(page.getByTestId('hover-test-running')).toBeVisible();
  await expect(page.getByTestId('hover-test-disabled')).toBeDisabled();
  await expect(page.getByTestId('hover-test-disabled')).toContainText('RUNNING');
  await page.getByTestId('hover-test-disabled').click({ force: true });
  await page.waitForTimeout(300);
  expect(calls).toHaveLength(1);

  release();
  await expect(page.getByTestId('hover-test-result')).toContainText('COMPLETED');
});

test('the control is not offered until the GCS has reported its mode', async ({ page }) => {
  let release;
  const modeDelay = new Promise((resolve) => { release = resolve; });
  await open(page, { modeDelay });
  await expect(page.getByText('FLIGHT OPERATIONS COMMAND')).toBeVisible();
  await page.waitForTimeout(500);
  await expect(page.getByTestId('hover-test')).toHaveCount(0);

  release();
  await expect(page.getByTestId('hover-test')).toBeVisible();
  await expect(button(page)).toBeEnabled();
});

test('in hardware mode the control is disabled and says why', async ({ page }) => {
  const calls = await open(page, { mode: { ...MODE, mode: 'HARDWARE', backend: 'hardware', fidelity: 'hardware' } });
  await expect(page.getByTestId('hover-test-reason')).toContainText('simulation only');
  await expect(page.getByTestId('hover-test-disabled')).toBeDisabled();
  expect(calls).toHaveLength(0);
});

test('a viewer sees the control disabled with the reason', async ({ page }) => {
  await open(page, { session: { ...TEST_SESSION, operator: 'e2e-viewer', role: 'VIEWER' } });
  await expect(page.getByTestId('hover-test-reason')).toContainText('FLIGHT_COMMANDER');
  await expect(page.getByTestId('hover-test-disabled')).toBeDisabled();
});

test('with no drone connected the control is disabled and says so', async ({ page }) => {
  await open(page, { drones: [{ connected: false }] });
  await expect(page.getByTestId('hover-test-reason')).toContainText('No drone is connected');
  await expect(page.getByTestId('hover-test-disabled')).toBeDisabled();
});
