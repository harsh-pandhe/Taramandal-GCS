import { expect, test } from '@playwright/test';

// Issue #48: the checklist modal must block the launch button while any
// pre-arm gate is red, and must never send /api/launch in that state.
//
// The GCS is replaced with page.route() handlers so each test controls the
// exact report the dashboard sees; the gate logic itself is covered by
// packages/tara-gcs/tests/test_preflight*.py.

const HARNESS = '/e2e/harness/preflight.html';

const RULES = [
  ['operator_latch', 'Operator confirmation latch', 'fleet'],
  ['fleet_complete', 'Full fleet connected', 'fleet'],
  ['connected', 'Vehicle link up', 'drone'],
  ['gnss_fix', 'GNSS / RTK fix', 'drone'],
  ['battery_soc', 'Battery state of charge', 'drone'],
  ['ekf_health', 'EKF health & home lock', 'drone'],
];

// Builds a /api/preflight-check response. `fail` maps a drone rule to the
// drone ids failing it (or a fleet rule to true); `severity` and
// `overridable` adjust individual rules the way a profile would.
function makeReport({
  profile = 'PRODUCTION',
  drones = [0, 1, 2],
  fail = {},
  severity = {},
  overridable = [],
  latch = 'idle',
} = {}) {
  const sev = (id) => severity[id] ?? 'BLOCKING';
  const state = (id, failed) => {
    if (sev(id) === 'IGNORED') return 'ignored';
    if (!failed) return 'pass';
    return sev(id) === 'WARNING' ? 'warn' : 'fail';
  };
  const gate = (id, title, failed) => ({
    rule: id, title, severity: sev(id), passed: !failed, state: state(id, failed),
    detail: failed ? `${title} failed` : 'ok',
  });

  const fleetGates = RULES.filter(([, , scope]) => scope === 'fleet')
    .map(([id, title]) => gate(id, title, Boolean(fail[id])));
  const droneReports = Object.fromEntries(drones.map((d) => {
    const gates = RULES.filter(([, , scope]) => scope === 'drone')
      .map(([id, title]) => gate(id, title, (fail[id] || []).includes(d)));
    return [String(d), { ready: !gates.some((g) => g.state === 'fail'), armed: false, gates }];
  }));

  const blocking = [
    ...fleetGates.filter((g) => g.state === 'fail').map((g) => `Fleet: ${g.title} — ${g.detail}`),
    ...Object.entries(droneReports).flatMap(([d, r]) =>
      r.gates.filter((g) => g.state === 'fail').map((g) => `Drone ${d}: ${g.title} — ${g.detail}`)),
  ];
  const permitted = blocking.length === 0;
  return {
    profile,
    launch_permitted: permitted,
    launch_drone_ids: permitted ? drones : [],
    excluded_drone_ids: [],
    blocking_reasons: blocking,
    rules: RULES.map(([id, title, scope]) => ({
      id, title, scope, severity: sev(id), overridable: overridable.includes(id), overridden: false,
    })),
    fleet_gates: fleetGates,
    drones: droneReports,
    confirmations: { latch: { state: latch }, override: { state: 'idle' } },
  };
}

// Serves `current.report` for every poll, and records every other /api/* call.
async function mockGcs(page, initialReport) {
  const current = { report: initialReport };
  const calls = [];
  await page.route('**/api/preflight-check', (route) => route.fulfill({ json: current.report }));
  await page.route('**/api/launch', (route) => {
    calls.push({ path: '/api/launch' });
    return route.fulfill({ json: { status: 'success', drone_ids: current.report.launch_drone_ids } });
  });
  await page.route('**/api/preflight/**', (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    calls.push({ path, body: req.postDataJSON() });
    if (path.endsWith('/request')) {
      return route.fulfill({ json: { token: `tok-${calls.length}`, expires_in_s: 30, rules: req.postDataJSON().rules ?? [] } });
    }
    return route.fulfill({ json: { confirmed: true, expires_in_s: 120, rules: [] } });
  });
  return { current, calls };
}

test('red gates block the launch button and never send /api/launch', async ({ page }) => {
  const { calls } = await mockGcs(page, makeReport({ fail: { gnss_fix: [1] } }));
  await page.goto(HARNESS);

  const launch = page.getByTestId('preflight-launch');
  await expect(page.getByTestId('interlock-gnss_fix')).toHaveAttribute('data-state', 'fail');
  await expect(launch).toBeDisabled();
  await expect(launch).toHaveText('LAUNCH BLOCKED');
  await expect(page.getByTestId('blocking-reasons')).toContainText('Drone 1: GNSS / RTK fix');

  await launch.click({ force: true });
  await page.waitForTimeout(300);
  expect(calls.filter((c) => c.path === '/api/launch')).toHaveLength(0);
});

test('all-green gates enable launch, and the launch reaches the GCS', async ({ page }) => {
  const { calls } = await mockGcs(page, makeReport());
  await page.goto(HARNESS);

  const launch = page.getByTestId('preflight-launch');
  await expect(launch).toBeEnabled();
  await expect(launch).toHaveText('LAUNCH FLEET');
  await launch.click();
  await expect(page.getByTestId('launched')).toHaveText('0,1,2');
  expect(calls.filter((c) => c.path === '/api/launch')).toHaveLength(1);
});

test('a gate turning red while the modal is open re-locks launch', async ({ page }) => {
  const { current } = await mockGcs(page, makeReport());
  await page.goto(HARNESS);
  const launch = page.getByTestId('preflight-launch');
  await expect(launch).toBeEnabled();

  current.report = makeReport({ fail: { battery_soc: [2] } });
  await expect(launch).toBeDisabled();
  await expect(page.getByTestId('interlock-battery_soc')).toHaveAttribute('data-state', 'fail');
});

test('an unreachable GCS locks launch rather than keeping the last green report', async ({ page }) => {
  const { current } = await mockGcs(page, makeReport());
  await page.goto(HARNESS);
  const launch = page.getByTestId('preflight-launch');
  await expect(launch).toBeEnabled();

  current.report = null;
  await page.unroute('**/api/preflight-check');
  await page.route('**/api/preflight-check', (route) => route.fulfill({ status: 503, body: 'down' }));
  await expect(launch).toBeDisabled();
  await expect(page.getByTestId('interlock-unavailable')).toBeVisible();
});

test('the launch latch is a two-step confirmation', async ({ page }) => {
  const { current, calls } = await mockGcs(page, makeReport({ fail: { operator_latch: true } }));
  await page.goto(HARNESS);
  await expect(page.getByTestId('preflight-launch')).toBeDisabled();

  await page.getByTestId('latch-request').click();
  await expect(page.getByTestId('latch-confirm')).toBeVisible();
  // Step 1 alone must not unlock anything.
  await expect(page.getByTestId('preflight-launch')).toBeDisabled();

  await page.getByTestId('latch-confirm').click();
  const paths = calls.map((c) => c.path);
  expect(paths).toEqual(['/api/preflight/latch/request', '/api/preflight/latch/confirm']);
  expect(calls[1].body.token).toBe('tok-1');

  current.report = makeReport({ latch: 'confirmed' });
  await expect(page.getByTestId('preflight-launch')).toBeEnabled();
});

test('an override needs a reason and two separate confirmations', async ({ page }) => {
  const { calls } = await mockGcs(page, makeReport({
    profile: 'ENGINEERING', fail: { ekf_health: [0] }, overridable: ['ekf_health'],
    severity: { operator_latch: 'IGNORED' },
  }));
  await page.goto(HARNESS);

  const request = page.getByTestId('override-request');
  await expect(request).toBeDisabled();
  await page.getByTestId('override-reason').fill('bench test, no GNSS antenna');
  await request.click();
  await page.getByTestId('override-confirm').click();

  expect(calls.map((c) => c.path)).toEqual(['/api/preflight/override/request', '/api/preflight/override/confirm']);
  expect(calls[0].body).toMatchObject({ rules: ['ekf_health'], reason: 'bench test, no GNSS antenna' });
  await expect(page.getByTestId('preflight-message')).toHaveText('Override confirmed and logged.');
  // Launch stays locked until the GCS itself reports the gate overridden.
  await expect(page.getByTestId('preflight-launch')).toBeDisabled();
});
