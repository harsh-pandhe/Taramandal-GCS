import { expect, test } from '@playwright/test';
import { signIn } from './session.js';

// Replay mode (issue #65): the REPLAY tab lists recorded flights from the GCS's
// read-only /api/blackbox/* routes, plays one in the night-sky view and pins the
// safety events on a timeline. The GCS is mocked here with the real response
// shapes (tara_gcs/blackbox_replay.py); the replay API itself is tested in
// packages/tara-gcs/tests/test_blackbox_replay.py.

const SESSION = {
  id: 'blackbox-20261110-101500',
  bytes: 4096,
  modified: 1794000000,
  started: '2026-11-10T10:15:00Z',
  context: {},
  live: false,
};

// 3 drones, one frame per second for a minute; drone k flies east at 1 m/s.
const FRAMES = Array.from({ length: 61 }, (_, s) => ({
  t_ms: s * 1000,
  ids: [0, 1, 2],
  x_m: [0, 1, 2].map((k) => s + k * 10),
  y_m: [0, 5, 10],
  z_m: [10, 10, 10],
  heading_deg: [90, 90, 90],
  flags: [3, 3, 1],
}));

const EVENTS = [
  { type: 'event', t: 20000, kind: 'proximity_breach', drones: [0, 1], distance_m: 1.4 },
  { type: 'event', t: 25000, kind: 'proximity_clear', drones: [0, 1] },
  { type: 'event', t: 40000, kind: 'geofence_hard_breach', drone: 2, distance_m: 61.5, limit_m: 60 },
];

// Commands and lifecycle events, as the GCS answers `events?kinds=command,fsm_transition,fsm_rejected`.
const ACTIVITY = [
  { type: 'event', t: 5000, kind: 'command', command: '/api/launch', trigger: 'POST /api/launch', accepted: true, status: 200 },
  { type: 'event', t: 8000, kind: 'fsm_transition', from: 'STAGED', to: 'ARMED', operator: 'operator-1a2b', trigger: 'api' },
  { type: 'event', t: 30000, kind: 'fsm_rejected', command: 'start', state: 'ARMED', reason: 'preflight not passed', operator: 'operator-1a2b', trigger: 'api' },
  { type: 'event', t: 45000, kind: 'command', command: '/api/land', trigger: 'POST /api/land', accepted: false, status: 409 },
];

const SUMMARY = {
  id: SESSION.id,
  header: {},
  identities_hidden: true,
  duration_ms: 60000,
  first_tick_ms: 0,
  ticks: 601, // 10 Hz: one tick every 100 ms
  drone_ids: [0, 1, 2],
  events: 7,
  event_kinds: {},
  flag_bits: { connected: 0, armed: 1 },
  truncated: false,
  bytes: 4096,
};

// What the GCS answers for a window of every recorded tick: 100 ms apart.
function windowReply(fromMs, toMs) {
  const frames = [];
  for (let t = fromMs; t <= toMs; t += 100) {
    const s = t / 1000;
    frames.push({ t_ms: t, ids: [0, 1, 2], x_m: [0, 1, 2].map((k) => s + k * 10), y_m: [0, 5, 10], z_m: [10, 10, 10], heading_deg: [90, 90, 90], flags: [3, 3, 1] });
  }
  return { stride: 1, count: frames.length, t_ms_first: fromMs, t_ms_last: toMs, frames };
}

async function openReplay(page, {
  sessions = [SESSION],
  frames = { stride: 10, count: 61, t_ms_first: 0, t_ms_last: 60000, frames: FRAMES },
  sessionStatus = 200,
  windowStatus = 200,
  activity = ACTIVITY,
  activityStatus = 200,
} = {}) {
  const requested = [];
  requested.times = []; // Date.now() of each request, parallel to `requested`
  await signIn(page);
  await page.routeWebSocket('**/ws/telemetry', () => {});
  await page.route('**/choreography.json', (route) => route.fulfill({ json: { drones: [] } }));
  await page.route('**/api/blackbox/**', (route) => {
    const url = new URL(route.request().url());
    requested.push(url.pathname + url.search);
    requested.times.push(Date.now());
    if (url.pathname === '/api/blackbox/sessions') return route.fulfill({ json: { sessions } });
    if (sessionStatus !== 200) return route.fulfill({ status: sessionStatus, json: { detail: 'no such session' } });
    if (url.pathname.endsWith('/events') && url.searchParams.has('kinds')) {
      if (activityStatus !== 200) return route.fulfill({ status: activityStatus, json: { detail: 'events unavailable' } });
      return route.fulfill({ json: { events: activity, total: activity.length, limited: false, identities_hidden: true } });
    }
    if (url.pathname.endsWith('/events')) return route.fulfill({ json: { events: EVENTS, total: 3, limited: false, identities_hidden: true } });
    if (url.pathname.endsWith('/frames') && url.searchParams.has('from_ms')) {
      if (windowStatus !== 200) return route.fulfill({ status: windowStatus, json: { detail: windowStatus === 401 ? 'session expired' : 'window failed' } });
      return route.fulfill({ json: windowReply(Number(url.searchParams.get('from_ms')), Number(url.searchParams.get('to_ms'))) });
    }
    if (url.pathname.endsWith('/frames')) return route.fulfill({ json: frames });
    return route.fulfill({ json: SUMMARY });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'REPLAY' }).click();
  return requested;
}

test('lists the recordings and loads one with its safety events on the timeline', async ({ page }) => {
  const requested = await openReplay(page);

  await expect(page.getByLabel('RECORDING')).toContainText('2026');
  await page.getByRole('button', { name: 'LOAD' }).click();

  await expect(page.getByTestId('replay-clock')).toHaveText('00:00 / 01:00');
  // A recording has no planned trajectory, so the live view's deviation HUD is not shown.
  await expect(page.getByTestId('deviation-hud')).toHaveCount(0);
  await expect(page.getByText('3 drones · 7 events recorded')).toBeVisible();
  await expect(page.getByRole('button', { name: /proximity breach at 00:20/ })).toBeVisible();
  await expect(page.getByRole('button', { name: /geofence hard breach at 00:40/ })).toBeVisible();
  await expect(page.getByText('3 safety events on the timeline')).toBeVisible();
  // The markers come from the safety filter, not from the whole event list.
  expect(requested.some((r) => r.includes('/events?safety_only=true'))).toBe(true);
});

test('clicking a marker jumps to that moment and shows the event', async ({ page }) => {
  await openReplay(page);
  await page.getByRole('button', { name: 'LOAD' }).click();

  await page.getByRole('button', { name: /geofence hard breach at 00:40/ }).click();

  await expect(page.getByTestId('replay-clock')).toHaveText('00:40 / 01:00');
  await expect(page.getByLabel('Timeline')).toHaveValue('40000');
  const detail = page.getByTestId('replay-detail');
  await expect(detail).toContainText('GEOFENCE HARD BREACH');
  await expect(detail).toContainText('distance_m');
  await expect(detail).toContainText('61.5');
  // Internal record fields are not shown as event details.
  await expect(detail).not.toContainText('wall');
});

test('play advances the clock, and scrubbing stops playback', async ({ page }) => {
  await openReplay(page);
  await page.getByRole('button', { name: 'LOAD' }).click();

  await page.getByLabel('Playback speed').selectOption('10');
  await page.getByRole('button', { name: 'Play', exact: true }).click();
  await expect.poll(async () => Number(await page.getByLabel('Timeline').inputValue()), { timeout: 10_000 }).toBeGreaterThan(5000);

  await page.getByLabel('Timeline').fill('30000');
  await expect(page.getByTestId('replay-clock')).toHaveText('00:30 / 01:00');
  await expect(page.getByRole('button', { name: 'Play', exact: true })).toBeVisible();
});

test('says so when there are no recordings', async ({ page }) => {
  await openReplay(page, { sessions: [] });
  await expect(page.getByText(/No recorded flights yet/)).toBeVisible();
});

test('a recording the GCS refuses is reported with its reason, not shown as empty', async ({ page }) => {
  await openReplay(page, { sessionStatus: 404 });
  await page.getByRole('button', { name: 'LOAD' }).click();
  await expect(page.getByText('Could not load this recording: no such session')).toBeVisible();
  await expect(page.getByTestId('replay-clock')).toHaveCount(0);
});

test('after a seek, a window of every recorded tick is loaded around that time and the view says so', async ({ page }) => {
  const requested = await openReplay(page);
  await page.getByRole('button', { name: 'LOAD' }).click();
  await expect(page.getByTestId('replay-resolution')).toContainText('Overview: one sample per 1.0 s');

  await page.getByRole('button', { name: /geofence hard breach at 00:40/ }).click();

  await expect(page.getByTestId('replay-resolution')).toContainText('Showing every recorded sample from');
  const windowRequests = requested.filter((r) => r.includes('from_ms='));
  expect(windowRequests).toHaveLength(1);
  // Asks for every tick (no step) with the API's hard caps, for a window that contains the time.
  expect(windowRequests[0]).toMatch(/max_frames=5000&max_samples=200000/);
  expect(windowRequests[0]).not.toContain('step_ms');
  const from = Number(/from_ms=(\d+)/.exec(windowRequests[0])[1]);
  const to = Number(/to_ms=(\d+)/.exec(windowRequests[0])[1]);
  expect(from).toBeLessThanOrEqual(40000);
  expect(to).toBeGreaterThanOrEqual(40000);
});

test('a failed window keeps the overview, shows no error, and is not asked for again straight away', async ({ page }) => {
  const requested = await openReplay(page, { windowStatus: 500 });
  await page.getByRole('button', { name: 'LOAD' }).click();

  for (const label of [/proximity breach at 00:20/, /proximity clear at 00:25/, /geofence hard breach at 00:40/]) {
    await page.getByRole('button', { name: label }).click();
    await page.waitForTimeout(450);
  }

  await expect(page.getByTestId('replay-resolution')).toContainText('Overview: one sample per 1.0 s');
  await expect(page.getByText(/Could not load/)).toHaveCount(0);
  // Asked for at least once, and never again sooner than the 5 s retry wait. The spacing is checked
  // rather than a count, so a slow machine that lets the wait run out mid-test is not a failure.
  const times = requested.times.filter((_, i) => requested[i].includes('from_ms='));
  expect(times.length).toBeGreaterThanOrEqual(1);
  for (let i = 1; i < times.length; i++) expect(times[i] - times[i - 1]).toBeGreaterThanOrEqual(4500);
});

test('the full-rate window helpers: size, placement, coverage and which frames get drawn', async ({ page }) => {
  await openReplay(page);
  const r = await page.evaluate(async () => {
    const m = await import('/src/replay.js');
    const win = { from: 10000, to: 40000, frames: [{ t_ms: 10000 }, { t_ms: 10100 }] };
    return {
      span100: m.detailSpanMs(100, 100), // 300 frames x 100 ms
      span1000: m.detailSpanMs(1000, 100), // 30 frames x 100 ms = 3 s, the floor
      span3: m.detailSpanMs(3, 100), // capped at 60 s
      tick: m.tickIntervalMs({ first_tick_ms: 0, duration_ms: 60000, ticks: 601 }),
      tickUnknown: m.tickIntervalMs({}),
      early: m.detailWindowFor(1000, 30000, 0, 900000),
      middle: m.detailWindowFor(300000, 30000, 0, 900000),
      late: m.detailWindowFor(895000, 30000, 0, 900000),
      covers: [m.windowCovers(win, 20000), m.windowCovers(win, 9999), m.windowCovers(win, 40001), m.windowCovers(win, 39000, 2000), m.windowCovers(null, 1)],
      oneFrame: m.windowCovers({ from: 0, to: 10, frames: [{ t_ms: 0 }] }, 5),
      picked: [m.pickFrames(['overview'], win, 20000) === win.frames, m.pickFrames(['overview'], win, 50000)[0]],
    };
  });
  expect(r.span100).toBe(30000);
  expect(r.span1000).toBe(3000);
  expect(r.span3).toBe(60000);
  expect(r.tick).toBe(100);
  expect(r.tickUnknown).toBe(100);
  expect(r.early).toEqual({ from: 0, to: 30000 });
  expect(r.middle).toEqual({ from: 292500, to: 322500 }); // a quarter of the span before the time
  expect(r.late).toEqual({ from: 870000, to: 900000 }); // kept inside the session
  expect(r.covers).toEqual([true, false, false, false, false]);
  expect(r.oneFrame).toBe(false);
  expect(r.picked).toEqual([true, 'overview']);
});

test('the replay can be shown on the Earth view and switched back, without errors', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  // The Earth view asks OpenStreetMap for imagery; the test does not depend on the internet.
  await page.route('**/tile.openstreetmap.org/**', (route) => route.abort());
  await openReplay(page);
  await page.getByRole('button', { name: 'LOAD' }).click();
  await expect(page.getByTestId('replay-clock')).toHaveText('00:00 / 01:00');

  const group = page.getByRole('group', { name: '3D view' });
  await expect(group.getByRole('button', { name: 'NIGHT SKY' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.cesium-viewer')).toHaveCount(0);

  await group.getByRole('button', { name: 'EARTH' }).click();
  await expect(group.getByRole('button', { name: 'EARTH' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('replay-stage').locator('.cesium-viewer')).toBeVisible();

  // The same controls drive it: jump to a marker, play, scrub.
  await page.getByRole('button', { name: /geofence hard breach at 00:40/ }).click();
  await expect(page.getByTestId('replay-clock')).toHaveText('00:40 / 01:00');
  await page.getByRole('button', { name: 'Play', exact: true }).click();
  await page.waitForTimeout(600);
  await page.getByLabel('Timeline').fill('10000');
  await expect(page.getByTestId('replay-clock')).toHaveText('00:10 / 01:00');

  await group.getByRole('button', { name: 'NIGHT SKY' }).click();
  await expect(page.locator('.cesium-viewer')).toHaveCount(0);
  await expect(page.getByTestId('replay-stage')).toBeVisible();
  expect(errors).toEqual([]);
});

test('telemetryAt gives the Earth view the fleet at a time: blended positions, flags as booleans, keyed by drone id', async ({ page }) => {
  await openReplay(page);
  const r = await page.evaluate(async (frames) => {
    const { telemetryAt } = await import('/src/replay.js');
    return { at: telemetryAt(frames, 2500), start: telemetryAt(frames, 0), empty: telemetryAt([], 5) };
  }, FRAMES);

  expect(Object.keys(r.at)).toEqual(['0', '1', '2']);
  expect(r.at['0']).toEqual({ id: 0, fleet_x: 2.5, fleet_y: 0, fleet_z: 10, heading: 90, armed: true, connected: true });
  expect(r.at['1'].fleet_x).toBe(12.5); // x = s + 10k, halfway between s = 2 and s = 3
  expect(r.at['2']).toMatchObject({ fleet_x: 22.5, fleet_y: 10, armed: false, connected: true }); // flags 1: connected, not armed
  expect(r.start['2'].fleet_x).toBe(20);
  expect(r.empty).toEqual({});
});

test('commands and lifecycle events are off until asked for, then load once and show as a second row of markers', async ({ page }) => {
  const requested = await openReplay(page);
  await page.getByRole('button', { name: 'LOAD' }).click();
  await expect(page.getByTestId('replay-clock')).toHaveText('00:00 / 01:00');
  const toggle = page.getByRole('button', { name: 'COMMANDS & LIFECYCLE' });
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  await expect(page.getByTestId('replay-activity-markers')).toHaveCount(0);
  expect(requested.some((r) => r.includes('kinds='))).toBe(false);

  await toggle.click();
  await expect(page.getByTestId('replay-activity-markers').getByRole('button')).toHaveCount(4);
  await expect(page.getByTestId('replay-activity-note')).toHaveText('4 of 4 shown');
  const asked = requested.filter((r) => r.includes('kinds='));
  expect(asked).toHaveLength(1);
  expect(asked[0]).toContain('kinds=command,fsm_transition,fsm_rejected');
  expect(asked[0]).toContain('limit=2000');
  // The safety markers are still there, and refusals are told apart from what the GCS accepted.
  await expect(page.getByRole('button', { name: /proximity breach at 00:20/ })).toBeVisible();
  await expect(page.getByTestId('replay-activity-markers').locator('.refused')).toHaveCount(2);

  // Off hides them; on again does not ask the GCS again.
  await toggle.click();
  await expect(page.getByTestId('replay-activity-markers')).toHaveCount(0);
  await toggle.click();
  await expect(page.getByTestId('replay-activity-markers').getByRole('button')).toHaveCount(4);
  expect(requested.filter((r) => r.includes('kinds='))).toHaveLength(1);
});

test('clicking a lifecycle marker jumps there and says what happened', async ({ page }) => {
  await openReplay(page);
  await page.getByRole('button', { name: 'LOAD' }).click();
  await page.getByRole('button', { name: 'COMMANDS & LIFECYCLE' }).click();

  await page.getByRole('button', { name: 'STAGED -> ARMED at 00:08' }).click();
  await expect(page.getByTestId('replay-clock')).toHaveText('00:08 / 01:00');
  const detail = page.getByTestId('replay-detail');
  await expect(detail).toContainText('STAGED -> ARMED');
  await expect(detail).toContainText('operator-1a2b'); // the GCS has already pseudonymised it
  await page.getByRole('button', { name: 'refused start: preflight not passed at 00:30' }).click();
  await expect(detail).toContainText('preflight not passed');
  await page.getByRole('button', { name: '/api/land (refused) at 00:45' }).click();
  await expect(page.getByTestId('replay-clock')).toHaveText('00:45 / 01:00');
});

test('says so when a recording has no commands', async ({ page }) => {
  await openReplay(page, { activity: [] });
  await page.getByRole('button', { name: 'LOAD' }).click();
  await page.getByRole('button', { name: 'COMMANDS & LIFECYCLE' }).click();
  await expect(page.getByTestId('replay-activity-note')).toHaveText('No commands or lifecycle changes were recorded.');
});

test('a failed load of the commands is reported and does not touch the rest of the replay', async ({ page }) => {
  await openReplay(page, { activityStatus: 500 });
  await page.getByRole('button', { name: 'LOAD' }).click();
  await page.getByRole('button', { name: 'COMMANDS & LIFECYCLE' }).click();
  await expect(page.getByText('Could not load them: events unavailable')).toBeVisible();
  await expect(page.getByRole('button', { name: /proximity breach at 00:20/ })).toBeVisible();
  await expect(page.getByTestId('replay-clock')).toBeVisible();
});

test('activityText says what an event was', async ({ page }) => {
  await openReplay(page);
  const r = await page.evaluate(async () => {
    const { activityText, isRefusal } = await import('/src/replay.js');
    return {
      transition: activityText({ kind: 'fsm_transition', from: 'A', to: 'B' }),
      noFields: activityText({ kind: 'fsm_transition' }),
      refused: activityText({ kind: 'fsm_rejected', command: 'start', reason: 'no' }),
      refusedBare: activityText({ kind: 'fsm_rejected' }),
      accepted: activityText({ kind: 'command', command: '/api/land', accepted: true }),
      refusedCommand: activityText({ kind: 'command', command: '/api/land', accepted: false }),
      other: activityText({ kind: 'some_thing' }),
      refusals: [isRefusal({ kind: 'fsm_rejected' }), isRefusal({ kind: 'command', accepted: false }), isRefusal({ kind: 'command', accepted: true }), isRefusal({ kind: 'fsm_transition' })],
    };
  });
  expect(r).toEqual({
    transition: 'A -> B', noFields: '? -> ?', refused: 'refused start: no', refusedBare: 'refused command',
    accepted: '/api/land', refusedCommand: '/api/land (refused)', other: 'some thing',
    refusals: [true, true, false, false],
  });
});

// --- the full-rate windows are fetched and parsed in a worker (issue #284) ---------------------------

const spyOnWorkers = (page) => page.addInitScript(() => {
  const Real = window.Worker;
  window.__workersStarted = 0;
  window.Worker = function SpyWorker(...args) {
    window.__workersStarted += 1;
    return new Real(...args);
  };
});

test('a full-rate window is fetched by a worker and still drives the view', async ({ page }) => {
  await spyOnWorkers(page);
  const requested = await openReplay(page);
  await page.getByRole('button', { name: 'LOAD' }).click();
  await page.getByRole('button', { name: /geofence hard breach at 00:40/ }).click();

  await expect(page.getByTestId('replay-resolution')).toContainText('Showing every recorded sample from');
  expect(await page.evaluate(() => window.__workersStarted)).toBe(1);
  expect(requested.filter((r) => r.includes('from_ms='))).toHaveLength(1);
  // The window's frames are typed-array views now; playback and the Earth view read them like the JSON arrays.
  await page.getByRole('button', { name: 'Play', exact: true }).click();
  await page.waitForTimeout(500);
  await page.getByRole('group', { name: '3D view' }).getByRole('button', { name: 'EARTH' }).click();
  await expect(page.getByTestId('replay-stage').locator('.cesium-viewer')).toBeVisible();
});

test('without Worker support the window is fetched on the main thread, as before', async ({ page }) => {
  await page.addInitScript(() => { delete window.Worker; });
  const requested = await openReplay(page);
  await page.getByRole('button', { name: 'LOAD' }).click();
  await page.getByRole('button', { name: /geofence hard breach at 00:40/ }).click();
  await expect(page.getByTestId('replay-resolution')).toContainText('Showing every recorded sample from');
  expect(requested.filter((r) => r.includes('from_ms='))).toHaveLength(1);
});

test('a worker that cannot start falls back to the main thread', async ({ page }) => {
  await page.addInitScript(() => {
    window.Worker = function BrokenWorker() { throw new Error('no workers here'); };
  });
  await openReplay(page);
  await page.getByRole('button', { name: 'LOAD' }).click();
  await page.getByRole('button', { name: /geofence hard breach at 00:40/ }).click();
  await expect(page.getByTestId('replay-resolution')).toContainText('Showing every recorded sample from');
});

test('an expired session seen by the worker ends the session, like every other GCS call', async ({ page }) => {
  await openReplay(page, { windowStatus: 401 });
  await page.getByRole('button', { name: 'LOAD' }).click();
  await page.getByRole('button', { name: /geofence hard breach at 00:40/ }).click();
  await expect(page.getByRole('button', { name: 'SIGN IN' })).toBeVisible();
});

test('packing a window into typed arrays and back keeps what the replay reads', async ({ page }) => {
  await openReplay(page);
  const r = await page.evaluate(async (frames) => {
    const { packFrames, unpackFrames, packedBuffers } = await import('/src/replayPack.js');
    const { fillFleetBuffer, telemetryAt } = await import('/src/replay.js');
    const { FLOATS_PER_DRONE } = await import('/src/telemetry.js');
    // Frames whose fleet changes size, one without heading or flags, to cover the uneven cases.
    const odd = [
      { t_ms: 0, ids: [0, 1, 2], x_m: [1, 2, 3], y_m: [4, 5, 6], z_m: [7, 8, 9], heading_deg: [10, 20, 30], flags: [3, 3, 1] },
      { t_ms: 100, ids: [0, 2], x_m: [1.5, 3.5], y_m: [4.5, 6.5], z_m: [7.5, 9.5] },
      { t_ms: 200, ids: [], x_m: [], y_m: [], z_m: [] },
    ];
    const packed = packFrames(odd);
    const back = unpackFrames(packed);
    const buffers = packedBuffers(packed);
    const same = (a, b) => { const A = { data: new Float32Array(10 * FLOATS_PER_DRONE), count: 0 }; const B = { data: new Float32Array(10 * FLOATS_PER_DRONE), count: 0 }; fillFleetBuffer(a, 50, A); fillFleetBuffer(b, 50, B); return A.count === B.count && A.data.every((v, i) => Math.abs(v - B.data[i]) < 1e-4); };
    return {
      counts: back.map((f) => f.ids.length),
      t: back.map((f) => f.t_ms),
      second: { ids: [...back[1].ids], x: [...back[1].x_m], heading: [...back[1].heading_deg], flags: [...back[1].flags] },
      transferable: buffers.length,
      sameFill: same(odd, back),
      sameFillAtFrame: same(frames, unpackFrames(packFrames(frames))),
      sameTelemetry: JSON.stringify(telemetryAt(odd, 50)) === JSON.stringify(telemetryAt(back, 50)),
      empty: unpackFrames(packFrames([])).length,
    };
  }, FRAMES);
  expect(r.counts).toEqual([3, 2, 0]);
  expect(r.t).toEqual([0, 100, 200]);
  expect(r.second).toEqual({ ids: [0, 2], x: [1.5, 3.5], heading: [0, 0], flags: [0, 0] }); // missing channels read as 0
  expect(r.transferable).toBe(8);
  expect(r.sameFill).toBe(true);
  expect(r.sameFillAtFrame).toBe(true);
  expect(r.sameTelemetry).toBe(true);
  expect(r.empty).toBe(0);
});

test('geofenceRadiusFor takes a recording\'s radius only when it is a usable number', async ({ page }) => {
  await openReplay(page);
  const r = await page.evaluate(async () => {
    const { geofenceRadiusFor } = await import('/src/replay.js');
    const tag = (v) => (v === undefined ? 'default' : v); // undefined = the view keeps its own default
    const odd = [0, -3, '60', null, undefined, NaN, Infinity, true];
    return {
      usable: [60, 63.95].map((v) => tag(geofenceRadiusFor({ geofence_radius_m: v }))),
      notUsable: odd.map((v) => tag(geofenceRadiusFor({ geofence_radius_m: v }))),
      noSummary: [tag(geofenceRadiusFor(null)), tag(geofenceRadiusFor({}))],
    };
  });
  expect(r.usable).toEqual([60, 63.95]);
  expect(r.notUsable.every((v) => v === 'default')).toBe(true);
  expect(r.noSummary).toEqual(['default', 'default']);
});

test('fillFleetBuffer blends positions between recorded frames and marks planned/LED data not valid', async ({ page }) => {
  await openReplay(page);
  const result = await page.evaluate(async (frames) => {
    const { fillFleetBuffer, frameIndexAt } = await import('/src/replay.js');
    const { FAST_FIELDS, FLOATS_PER_DRONE } = await import('/src/telemetry.js');
    const buffer = { data: new Float32Array(10 * FLOATS_PER_DRONE).fill(-7), count: 0 };
    const n = fillFleetBuffer(frames, 2500, buffer); // halfway between the 2 s and 3 s frames
    const at = (k, name) => buffer.data[k * FLOATS_PER_DRONE + FAST_FIELDS.indexOf(name)];
    return {
      n,
      count: buffer.count,
      x: [0, 1, 2].map((k) => at(k, 'fleet_x')),
      z: at(0, 'fleet_z'),
      armed: [0, 1, 2].map((k) => at(k, 'armed')),
      connected: [0, 1, 2].map((k) => at(k, 'connected')),
      plannedValid: at(0, 'planned_valid'),
      ledValid: at(0, 'led_valid'),
      before: frameIndexAt(frames, -5),
      after: frameIndexAt(frames, 10 ** 9),
      untouchedBeyondFleet: buffer.data[3 * FLOATS_PER_DRONE],
      none: fillFleetBuffer([], 0, buffer),
    };
  }, FRAMES);

  expect(result.n).toBe(3);
  expect(result.count).toBe(3);
  expect(result.x).toEqual([2.5, 12.5, 22.5]); // x = s + 10k, halfway between s=2 and s=3
  expect(result.z).toBe(10);
  expect(result.armed).toEqual([1, 1, 0]);
  expect(result.connected).toEqual([1, 1, 1]);
  expect(result.plannedValid).toBe(0);
  expect(result.ledValid).toBe(0);
  expect(result.before).toBe(0);
  expect(result.after).toBe(60);
  expect(result.untouchedBeyondFleet).toBe(-7);
  expect(result.none).toBe(0);
});
