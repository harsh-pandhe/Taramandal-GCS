import { expect, test } from '@playwright/test';
import { TEST_SESSION, signIn } from './session.js';

// Issue #64: the dashboard signs the operator in, sends the session token on
// every GCS call and on the telemetry socket, and drops back to sign-in when
// the GCS stops accepting it. The GCS is stubbed with page.route().

async function stubGcs(page, { loginOk = true } = {}) {
  const seen = [];
  // Registered first: Playwright tries routes newest-first, so the specific
  // stubs below win. Anything else answers like an unreachable GCS, which the
  // dashboard already handles; an empty `{}` would not be a real GCS reply.
  await page.route('**/api/**', (route) => {
    const req = route.request();
    seen.push({ path: new URL(req.url()).pathname, auth: req.headers().authorization || null });
    return route.fulfill({ status: 503, body: 'not stubbed in e2e' });
  });
  await page.route('**/api/auth/login', async (route) => {
    const body = route.request().postDataJSON();
    seen.push({ path: '/api/auth/login', auth: null, body });
    if (!loginOk || body.passphrase !== 'right passphrase') {
      return route.fulfill({ status: 401, json: { detail: 'Unknown operator or wrong passphrase.' } });
    }
    return route.fulfill({ json: { token: 'tok-123', operator: body.operator, role: 'FLIGHT_COMMANDER', expires_at: 0 } });
  });
  await page.route('**/api/auth/logout', (route) => {
    seen.push({ path: '/api/auth/logout', auth: route.request().headers().authorization || null });
    return route.fulfill({ json: { status: 'success' } });
  });
  await page.routeWebSocket('**/ws/telemetry', () => {});
  // After routeWebSocket, so this wraps Playwright's socket mock rather than
  // being replaced by it.
  // Record what the dashboard offers when it opens the socket.
  await page.addInitScript(() => {
    window.__wsOffers = [];
    const Real = window.WebSocket;
    window.WebSocket = function (url, protocols) {
      window.__wsOffers.push(protocols || []);
      return new Real(url, protocols);
    };
    window.WebSocket.prototype = Real.prototype;
    Object.assign(window.WebSocket, { CONNECTING: 0, OPEN: 1, CLOSING: 2, CLOSED: 3 });
  });
  return seen;
}

test('signs in, then sends the token on every GCS call and on the socket', async ({ page }) => {
  const seen = await stubGcs(page);
  await page.goto('/');

  await expect(page.getByRole('button', { name: 'SIGN IN' })).toBeVisible();
  await expect(page.getByText('TARAMANDAL GCS')).toBeVisible();
  // Nothing is fetched from the GCS before signing in.
  expect(seen.filter((r) => r.path !== '/api/auth/login')).toEqual([]);

  await page.getByLabel('Operator').fill('fiona');
  await page.getByLabel('Passphrase').fill('wrong passphrase');
  await page.getByRole('button', { name: 'SIGN IN' }).click();
  await expect(page.getByRole('alert')).toHaveText('Unknown operator or wrong passphrase.');
  await expect(page.getByLabel('Passphrase')).toHaveValue('');

  await page.getByLabel('Passphrase').fill('right passphrase');
  await page.getByRole('button', { name: 'SIGN IN' }).click();
  await expect(page.getByTestId('operator')).toContainText('fiona · FLIGHT_COMMANDER');

  await expect.poll(() => seen.filter((r) => r.path === '/api/preflight-check').length).toBeGreaterThan(0);
  const calls = seen.filter((r) => r.path !== '/api/auth/login');
  expect(calls.length).toBeGreaterThan(0);
  for (const call of calls) expect(call.auth, call.path).toBe('Bearer tok-123');

  await expect.poll(() => page.evaluate(() => window.__wsOffers)).toContainEqual(['tara.bearer', 'tok-123']);
});

test('a 401 from the GCS ends the session and returns to sign-in', async ({ page }) => {
  await stubGcs(page);
  await page.route('**/api/preflight-check', (route) =>
    route.fulfill({ status: 401, json: { detail: 'Session expired or invalid; log in again.' } }));
  await signIn(page);
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'SIGN IN' })).toBeVisible();
  expect(await page.evaluate(() => sessionStorage.getItem('tara_gcs_session'))).toBeNull();
});

test('a refused telemetry socket (1008) returns to sign-in', async ({ page }) => {
  await stubGcs(page);
  await page.routeWebSocket('**/ws/telemetry', (ws) => ws.close({ code: 1008, reason: 'Session expired' }));
  await signIn(page);
  await page.goto('/');
  await expect(page.getByRole('button', { name: 'SIGN IN' })).toBeVisible();
});

test('sign out revokes the session on the GCS and returns to sign-in', async ({ page }) => {
  const seen = await stubGcs(page);
  await signIn(page);
  await page.goto('/');
  await expect(page.getByTestId('operator')).toContainText(`${TEST_SESSION.operator} · ${TEST_SESSION.role}`);
  await page.getByRole('button', { name: 'SIGN OUT' }).click();
  await expect(page.getByRole('button', { name: 'SIGN IN' })).toBeVisible();
  const logout = seen.find((r) => r.path === '/api/auth/logout');
  expect(logout?.auth).toBe(`Bearer ${TEST_SESSION.token}`);
});

test('a 403 shows why the role cannot do it, without signing out', async ({ page }) => {
  await stubGcs(page);
  await page.route('**/api/sim/launch', (route) => route.fulfill({
    status: 403, json: { detail: 'vera is a VIEWER; this needs FLIGHT_COMMANDER or higher.' },
  }));
  await signIn(page, { token: 'v', operator: 'vera', role: 'VIEWER' });
  await page.goto('/');
  // A (re)connecting socket clears the banner; let it settle first, as
  // dashboard.spec.js does (dev-mode effects reconnect once after 2s).
  await expect(page.getByText('GCS LINK: CONNECTED')).toBeVisible({ timeout: 15_000 });
  await page.getByRole('button', { name: /LAUNCH GAZEBO SITL/ }).click();
  await expect(page.getByText('vera is a VIEWER; this needs FLIGHT_COMMANDER or higher.')).toBeVisible();
  await expect(page.getByTestId('operator')).toContainText('vera · VIEWER');
});
