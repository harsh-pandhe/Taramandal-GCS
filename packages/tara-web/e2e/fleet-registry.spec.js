import { expect, test } from '@playwright/test';
import { signIn } from './session.js';

// Issue #304: the fleet discovery registry in the fleet panel. GET /api/fleet/registry says which drones have announced
// themselves and which went quiet. STALE must read as "announcements stale", never as the flight telemetry's ONLINE/OFFLINE
// (a discovered drone is not connected to the flight path). Backend stubbed; telemetry over a mocked WebSocket.

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

const FLEET = { state: 'IDLE', description: 'idle', since: 0, seq: 1, allowed_commands: [], last_transition: null, launch_failures: {} };

const REGISTRY = {
  enabled: true, host: '127.0.0.1', port: 14590, stale_after_s: 1.5, online: 2, stale: 2, capacity: 1000, accepted: 120,
  rejected: { BAD_TAG: 3, MALFORMED: 1 },
  drones: [
    { drone_id: 0, pad_index: 0, hardware_uuid: 'a1b2c3d4e5f60718', state: 'ONLINE', age_s: 0.2, stale_count: 0 },
    { drone_id: 1, pad_index: 1, hardware_uuid: 'a1b2c3d4e5f60719', state: 'STALE', age_s: 4.25, stale_count: 1 },
    { drone_id: 2, pad_index: 2, hardware_uuid: 'a1b2c3d4e5f6071a', state: 'ONLINE', age_s: 0.4, stale_count: 0 },
    { drone_id: 7, pad_index: 7, hardware_uuid: 'a1b2c3d4e5f6071b', state: 'STALE', age_s: null, stale_count: 0 },
  ],
};

async function open(page, n, handler) {
  const drones = Array.from({ length: n }, () => ({ connected: true, armed: false }));
  await page.routeWebSocket('**/ws/telemetry', (ws) => {
    ws.send(fastFrame(drones));
    ws.send(slowFrame(drones, FLEET));
  });
  await page.route('**/api/mode', (route) => route.fulfill({ json: { mode: 'SIMULATION', backend: 'px4', fidelity: 'px4-sitl', telemetry_status: 'FRESH', hardware_flight_certified: false } }));
  await page.route('**/api/sim/status', (route) => route.fulfill({ json: {} }));
  await page.route('**/api/fleet/registry', handler);
  await signIn(page);
  const asked = page.waitForResponse((r) => r.url().includes('/api/fleet/registry'), { timeout: 10000 }).catch(() => null);
  await page.goto('/');
  await asked;
  await expect(page.getByTestId('fleet-state')).toBeVisible();
}
const strip = (page) => page.getByTestId('discovery-strip');

test('discovery off: nothing is shown', async ({ page }) => {
  await open(page, 3, (route) => route.fulfill({ json: { enabled: false } }));
  await page.waitForTimeout(500);
  await expect(strip(page)).toHaveCount(0);
});

test('an older GCS without the endpoint shows nothing and breaks nothing', async ({ page }) => {
  await open(page, 3, (route) => route.fulfill({ status: 404, json: { detail: 'Not Found' } }));
  await page.waitForTimeout(500);
  await expect(strip(page)).toHaveCount(0);
  await expect(page.getByText('DRONE 00')).toBeVisible();
});

test('the strip counts announcing and stale drones, names the quiet ones and says what is not in the flight path', async ({ page }) => {
  await open(page, 3, (route) => route.fulfill({ json: REGISTRY }));
  await expect(page.getByTestId('discovery-state')).toHaveText('ON');
  await expect(page.getByTestId('discovery-online')).toHaveText('2');
  await expect(page.getByTestId('discovery-stale')).toHaveText('2');
  await expect(page.getByTestId('discovery-not-in-flight')).toHaveText('1');       // drone 7 is announced, not flown
  const list = page.getByTestId('discovery-stale-list');
  await expect(list).toContainText('drone 01 \u00b7 quiet 4.25 s');
  await expect(list).toContainText('drone 07 \u00b7 quiet since the GCS started');
  await expect(strip(page)).toContainText('Rejected frames');
  await expect(strip(page)).toContainText('4');                                      // 3 bad tags + 1 malformed
  await expect(strip(page)).toContainText('Announcements do not connect a drone');
});

test('a card with stale announcements says so, in its own words, and still shows its telemetry state', async ({ page }) => {
  await open(page, 3, (route) => route.fulfill({ json: REGISTRY }));
  const stale = page.getByTestId('drone-1-announce-stale');
  await expect(stale).toContainText('ANNOUNCEMENTS STALE: quiet 4.25 s');
  await expect(page.getByTestId('drone-0-announce-stale')).toHaveCount(0);           // announcing: no mark
  await expect(page.getByTestId('drone-2-announce-stale')).toHaveCount(0);
  // The telemetry says ONLINE for every card; the registry mark does not change that.
  await expect(page.getByText('ONLINE', { exact: true })).toHaveCount(3);
});

test('in the matrix a stale drone\'s tile is marked and its title says why', async ({ page }) => {
  await open(page, 14, (route) => route.fulfill({ json: REGISTRY }));
  const tiles = page.locator('.status-tile');
  await expect(tiles).toHaveCount(14);
  await expect(page.locator('.status-tile.registry-stale')).toHaveCount(2);          // drones 1 and 7, the two STALE ones
  await expect(tiles.nth(1)).toHaveClass(/registry-stale/);
  await expect(tiles.nth(7)).toHaveClass(/registry-stale/);
  await expect(tiles.nth(2)).not.toHaveClass(/registry-stale/);                       // announcing: no mark
  await expect(tiles.nth(1)).toHaveAttribute('title', /announcements STALE/);
  await expect(tiles.nth(0)).not.toHaveClass(/registry-stale/);
});

test('a live readout turns to NO UPDATE when the GCS stops answering', async ({ page }) => {
  let dead = false;
  await open(page, 3, (route) => (dead ? route.abort() : route.fulfill({ json: REGISTRY })));
  await expect(page.getByTestId('discovery-state')).toHaveText('ON');
  dead = true;
  await expect(page.getByTestId('discovery-state')).toHaveText('NO UPDATE', { timeout: 15000 });
});
