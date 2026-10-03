import { expect, test } from '@playwright/test';
import { signIn } from './session.js';

// Behavioural smoke test for the operator dashboard (App.jsx and the
// components it is built from). The other specs here drive the night-sky
// harness page and never load the dashboard, so until this file nothing
// exercised it end to end. It was written against the pre-#99 monolith and
// must pass unchanged on the decomposed version; it asserts behaviour (what an
// operator sees and what reaches the backend), not markup, so #93's visual
// redesign can keep it.
//
// Telemetry arrives over a mocked WebSocket in the GCS's real wire format
// (tara_gcs.telemetry_codec); every REST endpoint is stubbed.

// Mirrors src/telemetry.js FAST_FIELDS.
const FAST_FIELDS = [
  'fleet_x', 'fleet_y', 'fleet_z', 'local_x', 'local_y', 'local_z', 'heading',
  'armed', 'connected', 'planned_x', 'planned_y', 'planned_z', 'planned_valid',
];

function fastFrame(drones) {
  const buf = Buffer.alloc(drones.length * FAST_FIELDS.length * 4);
  drones.forEach((d, i) => {
    FAST_FIELDS.forEach((field, j) => {
      const v = typeof d[field] === 'boolean' ? Number(d[field]) : (d[field] ?? 0);
      buf.writeFloatLE(v, (i * FAST_FIELDS.length + j) * 4);
    });
  });
  return buf;
}

function slowFrame(drones) {
  return JSON.stringify({
    type: 'slow',
    drones: Object.fromEntries(drones.map((d, i) => [String(i), {
      flight_mode: d.flight_mode ?? 'HOLD',
      battery_percent: d.battery_percent ?? 95,
      battery_voltage: d.battery_voltage ?? 16.4,
      gps_lock: d.gps_lock ?? true,
      satellites: d.satellites ?? 14,
      gps_fix_type: d.gps_fix_type ?? 'FIX_3D',
      comm_latency_ms: d.comm_latency_ms ?? 12,
      home_lat: 47.397, home_lon: 8.545, home_alt: 488,
      port: 14540 + i,
    }])),
  });
}

// A drone card's title reads "DRONE 07(Port 14547)": the port is a nested span.
const droneCard = (page, id) => page.getByText(new RegExp(`^DRONE ${id}\\b`));

const fleet = (n, over = () => ({})) => Array.from({ length: n }, (_, i) => ({
  connected: true, armed: false, local_x: i, local_y: 2 * i, local_z: 0, ...over(i),
}));

const CHOREO = {
  drones: [
    { id: 0, waypoints: [{ time: 0, x: 0, y: 0, z: 0 }, { time: 10, x: 0, y: 0, z: -5 }] },
    { id: 1, waypoints: [{ time: 0, x: 0, y: 2, z: 0 }, { time: 10, x: 0, y: 2, z: -5 }] },
  ],
};

/** Opens the dashboard with `drones` streaming in; returns the requests the backend received. */
async function openDashboard(page, drones, { launch = { status: 'success', message: 'ok' } } = {}) {
  const requests = [];
  const record = (route) => { requests.push(new URL(route.request().url()).pathname); };

  await page.routeWebSocket('**/ws/telemetry', (ws) => {
    ws.send(fastFrame(drones));
    ws.send(slowFrame(drones));
  });
  await page.route('**/choreography.json', (route) => route.fulfill({ json: CHOREO }));
  await page.route('**/api/sim/status', (route) => route.fulfill({ json: {
    running: true, num_drones: drones.length, cpu_percent: 42, memory_mb: 2048,
    heartbeat: { alive: true, connected: drones.length, expected: drones.length },
  } }));
  await page.route('**/api/sim/stop', (route) => { record(route); return route.fulfill({ json: {
    status: 'success', sim: { running: false } } }); });
  await page.route('**/api/preflight-check', (route) => route.fulfill({ json: {
    profile: 'SIMULATION',
    launch_permitted: true,
    launch_drone_ids: drones.map((_, i) => i),
    excluded_drone_ids: [],
    blocking_reasons: [],
    rules: [
      { id: 'operator_latch', title: 'Operator confirmation latch', scope: 'fleet', severity: 'BLOCKING', overridable: false, overridden: false },
      { id: 'fleet_complete', title: 'Full fleet connected', scope: 'fleet', severity: 'BLOCKING', overridable: false, overridden: false },
      { id: 'connected', title: 'Vehicle link up', scope: 'drone', severity: 'BLOCKING', overridable: false, overridden: false },
    ],
    fleet_gates: [
      { rule: 'operator_latch', title: 'Operator confirmation latch', severity: 'BLOCKING', passed: true, state: 'pass', detail: 'latched' },
      { rule: 'fleet_complete', title: 'Full fleet connected', severity: 'BLOCKING', passed: true, state: 'pass', detail: '3/3' },
    ],
    drones: Object.fromEntries(drones.map((_, i) => [String(i), {
      ready: true, armed: false, gates: [
        { rule: 'connected', title: 'Vehicle link up', severity: 'BLOCKING', passed: true, state: 'pass', detail: 'connected' },
      ],
    }])),
    confirmations: { latch: { state: 'confirmed' }, override: { state: 'idle' } },
  } }));
  await page.route('**/api/launch', (route) => {
    record(route);
    const status = launch.status === 'error' ? 412 : 200;
    return route.fulfill({ status, json: launch });
  });
  await page.route('**/api/verify-launch-geometry**', (route) => { record(route); return route.fulfill({ json: {
    all_passed: true,
    drones: { 0: { status: 'PASSED', distance_error_m: 0.1 }, 1: { status: 'PASSED', distance_error_m: 0.2 } },
  } }); });
  await page.route('**/api/start-trajectory', (route) => {
    record(route);
    requests.startBody = route.request().postDataJSON();
    return route.fulfill({ json: { status: 'success', message: 'started' } });
  });
  // Shaped like DroneManager.mission_status() (issue #47).
  await page.route('**/api/upload-mission', (route) => { record(route); return route.fulfill({ json: {
    staged: true, all_verified: true, verified_count: 2, assigned_count: 2, show_id: 'abcdef0123456789',
    drones: { 0: { status: 'VERIFIED', crc32: '1a2b3c4d', msg: 'ok' }, 1: { status: 'VERIFIED', crc32: '5e6f7a8b', msg: 'ok' } },
    launch_gate: { ok: true, reason: '' },
  } }); });
  await page.route('**/api/upload-trajectory', (route) => {
    record(route);
    requests.uploadBody = route.request().postDataBuffer().toString('latin1');
    return route.fulfill({ json: {
      status: 'success',
      drones_summary: { 0: { waypoints_count: 2, duration_seconds: 10 }, 1: { waypoints_count: 2, duration_seconds: 10 } },
      trajectory_data: { 0: CHOREO.drones[0].waypoints, 1: CHOREO.drones[1].waypoints },
      signature: { verified: true, signer: 'studio-ci', key_id: 'ab12cd34', signed_at: '2026-09-30T10:00:00Z' },
    } });
  });

  await signIn(page);
  await page.goto('/');
  // Generous: in dev mode React runs effects twice, so the first socket is
  // closed and the reconnect is scheduled 2s later, while the page may still be
  // compiling its 3D preview's shaders.
  await expect(page.getByText('GCS LINK: CONNECTED')).toBeVisible({ timeout: 15_000 });
  return requests;
}

test('a small fleet renders one detail card per drone with its telemetry', async ({ page }) => {
  await openDashboard(page, fleet(3, (i) => (i === 2 ? { armed: true } : {})));

  for (const id of ['00', '01', '02']) {
    await expect(droneCard(page, id)).toBeVisible();
  }
  await expect(page.getByText('(Port 14541)')).toBeVisible();
  await expect(page.getByText('ARMED', { exact: true })).toHaveCount(1);
  await expect(page.getByText('95% (16.4V)')).toHaveCount(3);
  await expect(page.getByText('Active Drones: 3/3')).toBeVisible();
  await expect(page.getByRole('button', { name: 'PRE-ARM CHECK & TAKEOFF' })).toBeVisible();
});

test('pre-arm check modal opens from flight operations and shows interlocks', async ({ page }) => {
  await openDashboard(page, fleet(3));
  await page.getByRole('button', { name: 'PRE-ARM CHECK & TAKEOFF' }).click();
  await expect(page.getByRole('heading', { name: 'PRE-ARM CHECKLIST' })).toBeVisible();
  await expect(page.getByTestId('preflight-launch')).toBeEnabled();
});

test('above twelve drones the fleet becomes a status matrix with a drill-down card', async ({ page }) => {
  await openDashboard(page, fleet(20, (i) => (i === 7 ? { armed: true } : {})));

  const tiles = page.locator('.status-matrix button');
  await expect(tiles).toHaveCount(20);
  await expect(page.getByText('Click a tile above to inspect that drone.')).toBeVisible();
  await expect(droneCard(page, '07')).toHaveCount(0);

  await page.getByTitle('Drone 7: ARMED').click();
  await expect(droneCard(page, '07')).toBeVisible();
  await expect(page.getByText('Click a tile above to inspect that drone.')).toHaveCount(0);
});

test('a refused command surfaces the backend message in the error banner', async ({ page }) => {
  const requests = await openDashboard(page, fleet(3), { launch: { status: 'error', message: 'Launch refused by interlock' } });

  await page.getByRole('button', { name: 'PRE-ARM CHECK & TAKEOFF' }).click();
  await page.getByTestId('preflight-launch').click();
  await expect(page.getByText('Launch refused by interlock')).toBeVisible();
  expect(requests).toContain('/api/launch');
});

test('the simulation bar reports the running sim and can terminate it', async ({ page }) => {
  const requests = await openDashboard(page, fleet(3));

  await expect(page.getByText('SITL: RUNNING')).toBeVisible();
  await expect(page.getByText('NODES: 3/3')).toBeVisible();
  await expect(page.getByText('CPU: 42%')).toBeVisible();
  await expect(page.getByText('RAM: 2.0 GB')).toBeVisible();

  await page.getByRole('button', { name: /TERMINATE SIM/ }).click();
  await expect.poll(() => requests).toContain('/api/sim/stop');
});

test('the trajectory planner verifies geometry and the fleet missions, then starts the show', async ({ page }) => {
  const requests = await openDashboard(page, fleet(2));

  await expect(page.getByText('choreography.json (Preloaded)')).toBeVisible();
  await expect(page.getByText('• STOPPED')).toBeVisible();
  const start = page.getByRole('button', { name: 'START SHOW' });
  await expect(start).toBeDisabled();

  await page.getByRole('button', { name: 'Verify' }).click();
  await expect(page.getByText('✓ READY')).toBeVisible();
  await expect(page.getByText('PASSED (0.2m)')).toBeVisible();
  // Onboard execution (#47) also needs every drone holding a verified mission.
  await expect(page.getByText('Not uploaded to the fleet yet — arming is locked.')).toBeVisible();
  await expect(start).toBeDisabled();

  await page.getByRole('button', { name: 'Upload', exact: true }).click();
  await expect(page.getByText('✓ 2/2 VERIFIED')).toBeVisible();
  await expect(page.getByText('VERIFIED (crc 5e6f7a8b)')).toBeVisible();

  await start.click();
  await expect(page.getByText('• RUNNING')).toBeVisible();
  expect(requests).toEqual(expect.arrayContaining(['/api/verify-launch-geometry', '/api/upload-mission', '/api/start-trajectory']));
  expect(requests.startBody).toMatchObject({ mode: 'LOCAL', execution: 'onboard' });
  await expect(page.getByRole('button', { name: '⏹ STOP SHOW' })).toBeEnabled();
});

test('a signed show upload sends the signature, reports the signer and stages the missions', async ({ page }) => {
  const requests = await openDashboard(page, fleet(2));

  // The show and its detached .sig.json are picked together (#49).
  await page.locator('input[type=file]').setInputFiles([
    { name: 'show.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(CHOREO)) },
    { name: 'show.json.sig.json', mimeType: 'application/json', buffer: Buffer.from('{"sig":"x"}') },
  ]);

  await expect(page.getByText('SIGNED BY studio-ci (key ab12cd34)')).toBeVisible();
  expect(requests.uploadBody).toContain('name="signature"');
  // Upload auto-runs the geometry check and stages the missions onto the fleet (#47).
  await expect.poll(() => requests).toEqual(expect.arrayContaining(['/api/verify-launch-geometry', '/api/upload-mission']));
  await expect(page.getByText('✓ 2/2 VERIFIED')).toBeVisible();
});

test('view tabs swap the operations dashboard for the night-sky view and back', async ({ page }) => {
  await openDashboard(page, fleet(3));

  await page.getByRole('button', { name: 'NIGHT SKY' }).click();
  await expect(droneCard(page, '00')).toBeHidden();
  await page.getByRole('button', { name: 'OPERATIONS' }).click();
  await expect(droneCard(page, '00')).toBeVisible();
});
