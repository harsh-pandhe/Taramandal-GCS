import { expect, test } from '@playwright/test';
import { signIn } from './session.js';

// Issue #299: the fleet reads RUNNING but the GCS is not playing the show (a fleet-wide land that every vehicle refused
// stops the playback and, rightly, does not advance the state). The GCS derives `playback_stopped` and sends it in the
// fleet_state of the websocket slow frame; the dashboard must say so, and only then. Backend stubbed; telemetry over a
// mocked WebSocket in the GCS's wire format.

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
const slowFrame = (drones, fleetState) => JSON.stringify({
  type: 'slow',
  drones: Object.fromEntries(drones.map((d, i) => [String(i), {
    flight_mode: 'OFFBOARD', battery_percent: 95, battery_voltage: 16.4, gps_lock: true, satellites: 14,
    gps_fix_type: 'FIX_3D', comm_latency_ms: 12, home_lat: 47.397, home_lon: 8.545, home_alt: 488, port: 14540 + i,
  }])),
  fleet_state: fleetState,
});
const RUNNING = { state: 'RUNNING', description: 'the show is playing', since: 0, seq: 5, allowed_commands: [], last_transition: null, launch_failures: {} };

async function open(page, first) {
  const drones = [{ connected: true, armed: true }];
  const sock = {};
  await page.routeWebSocket('**/ws/telemetry', (ws) => {
    sock.ws = ws;
    ws.send(fastFrame(drones));
    ws.send(slowFrame(drones, first));
  });
  await page.route('**/api/mode', (route) => route.fulfill({ json: { mode: 'SIMULATION', backend: 'px4', fidelity: 'px4-sitl', telemetry_status: 'FRESH' } }));
  await page.route('**/api/sim/status', (route) => route.fulfill({ json: {} }));
  await signIn(page);
  await page.goto('/');
  return { push: (fleetState) => sock.ws.send(slowFrame(drones, fleetState)) };
}

const badge = (page) => page.getByTestId('fleet-state');
const banner = (page) => page.getByTestId('playback-stopped-banner');

test('a show that is playing shows plain RUNNING and no banner', async ({ page }) => {
  await open(page, { ...RUNNING, playback_stopped: false });
  await expect(badge(page)).toHaveText('FLEET: RUNNING');
  await expect(banner(page)).toHaveCount(0);
});

test('a GCS that does not send the flag behaves as before', async ({ page }) => {
  await open(page, RUNNING);
  await expect(badge(page)).toHaveText('FLEET: RUNNING');
  await expect(banner(page)).toHaveCount(0);
});

test('RUNNING without playback is labelled SHOW STOPPED and the banner says what it means', async ({ page }) => {
  await open(page, { ...RUNNING, playback_stopped: true });
  await expect(badge(page)).toHaveText('FLEET: RUNNING · SHOW STOPPED');
  await expect(banner(page)).toBeVisible();
  await expect(banner(page)).toContainText('SHOW STOPPED');
  await expect(banner(page)).toContainText('neither flying the show nor landing');
});

test('the banner follows the flag live on one open connection, and clears when the state moves on', async ({ page }) => {
  const { push } = await open(page, { ...RUNNING, playback_stopped: false });
  await expect(banner(page)).toHaveCount(0);

  push({ ...RUNNING, seq: 6, playback_stopped: true });
  await expect(banner(page)).toBeVisible();
  await expect(badge(page)).toContainText('SHOW STOPPED');

  push({ ...RUNNING, state: 'LANDING', description: 'landing', seq: 7, playback_stopped: false });
  await expect(banner(page)).toHaveCount(0);
  await expect(badge(page)).toHaveText('FLEET: LANDING');
});
