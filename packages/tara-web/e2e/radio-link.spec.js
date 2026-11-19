import { expect, test } from '@playwright/test';
import { signIn } from './session.js';

// Issue #68: the serial radio's link health on the operations HUD. The GCS answers GET /api/radio-link with
// {"configured": false} when it has no radio, or the transport's link_health(). Backend stubbed.

const LINK = {
  configured: true, device: '/dev/ttyUSB0', baud: 57600, connected: true, uptime_s: 42.1, last_error: null,
  reconnects: 0, connect_failures: 0,
  downlink: { hz: 10, frames_sent: 421, overruns: 0, late_ticks: 0, missed_ticks: 0, serial_buffer_utilization: 0.01 },
  uplink: { frames_received: 90, crc_errors: 1, framing_errors: 2, bad_payloads: 0, unknown_types: 0, drones_reporting: 35, parse_mean_ms: 0.01, parse_max_ms: 0.2, callback_errors: 0 },
  radio: { rssi_dbm: -87, snr_db: 11, packet_loss_pct: 5, radio_tx_buffer_pct: 35, age_s: 0.4 },
};

async function open(page, handler) {
  await page.routeWebSocket('**/ws/telemetry', () => {});
  await page.route('**/api/mode', (route) => route.fulfill({ json: { mode: 'SIMULATION', backend: 'px4', fidelity: 'px4-sitl', telemetry_status: 'FRESH', hardware_flight_certified: false } }));
  await page.route('**/api/sim/status', (route) => route.fulfill({ json: {} }));
  await page.route('**/api/radio-link', handler);
  await signIn(page);
  const asked = page.waitForResponse((r) => r.url().includes('/api/radio-link') || r.status() === 0, { timeout: 10000 }).catch(() => null);
  await page.goto('/');
  await asked;                                   // the dashboard has asked the GCS about the radio at least once
}
const strip = (page) => page.getByTestId('radio-link-strip');

test('no radio configured: nothing is shown', async ({ page }) => {
  await open(page, (route) => route.fulfill({ json: { configured: false } }));
  await page.waitForTimeout(500);                // time for a (wrongly) rendered strip to appear
  await expect(page.getByRole('button', { name: /sign out|logout/i }).first()).toBeVisible();
  await expect(strip(page)).toHaveCount(0);
});

test('an older GCS without the endpoint shows nothing and breaks nothing', async ({ page }) => {
  await open(page, (route) => route.fulfill({ status: 404, json: { detail: 'Not Found' } }));
  await page.waitForTimeout(500);                // time for a (wrongly) rendered strip to appear
  await expect(page.getByRole('button', { name: /sign out|logout/i }).first()).toBeVisible();
  await expect(strip(page)).toHaveCount(0);
});

test('a live link shows UP and the modem\'s own numbers', async ({ page }) => {
  await open(page, (route) => route.fulfill({ json: LINK }));
  await expect(page.getByTestId('radio-link-state')).toHaveText('UP');
  await expect(page.getByTestId('radio-rssi')).toHaveText('-87 dBm');
  await expect(page.getByTestId('radio-loss')).toHaveText('5 %');
  await expect(page.getByTestId('radio-overruns')).toHaveText('0');
  await expect(strip(page)).toContainText('35');            // drones reporting
  await expect(strip(page)).toContainText('3');             // uplink errors: 1 CRC + 2 framing
});

test('a dead link says DOWN and why, and shows no radio numbers it does not have', async ({ page }) => {
  await open(page, (route) => route.fulfill({ json: { ...LINK, connected: false, uptime_s: null, last_error: 'read failed: Input/output error', radio: null } }));
  await expect(page.getByTestId('radio-link-state')).toHaveText('DOWN');
  await expect(page.getByTestId('radio-link-error')).toContainText('Input/output error');
  await expect(page.getByTestId('radio-rssi')).toHaveText('—');
});

test('a link up but with no modem report yet says so instead of showing zeros', async ({ page }) => {
  await open(page, (route) => route.fulfill({ json: { ...LINK, radio: null } }));
  await expect(page.getByTestId('radio-link-state')).toHaveText('UP');
  await expect(page.getByTestId('radio-no-report')).toBeVisible();
  await expect(page.getByTestId('radio-loss')).toHaveText('—');
});

test('overruns are flagged', async ({ page }) => {
  await open(page, (route) => route.fulfill({ json: { ...LINK, downlink: { ...LINK.downlink, overruns: 7 } } }));
  await expect(page.getByTestId('radio-overruns')).toHaveText('7');
});

test('when the GCS stops answering, a live readout turns to NO UPDATE', async ({ page }) => {
  let dead = false;
  await open(page, (route) => (dead ? route.abort() : route.fulfill({ json: LINK })));
  await expect(page.getByTestId('radio-link-state')).toHaveText('UP');
  dead = true;
  await expect(page.getByTestId('radio-link-state')).toHaveText('NO UPDATE', { timeout: 15000 });
});
