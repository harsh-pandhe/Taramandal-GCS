import fs from 'node:fs';
import { test } from '@playwright/test';
import { signIn } from './session.js';

// Replay performance at 100 drones x 15 minutes (issue #65). Not part of the
// normal run: it needs a recording made by tools/replay_perf_record.py and is
// skipped without one, because what it measures depends on the machine's GPU and
// display (see docs/replay-benchmark.md for what it does and does not show).
//
//   python tools/replay_perf_record.py --out /some/dir
//   REPLAY_PERF_DATA=/some/dir/overview.json npx playwright test e2e/replay-perf.spec.js
//
// Optional: REPLAY_PERF_WINDOW=/some/dir/window30.json (a 30 s full-rate window, as
// tools/replay_perf_record.py writes it) makes the mocked GCS answer full-rate window
// requests with that window shifted to the time asked for, so the cost of loading and
// swapping windows is part of the measurement. Without it window requests fail, and the
// replay stays on the overview.
//
// The numbers are printed as JSON and attached to the test.

const DATA_PATH = process.env.REPLAY_PERF_DATA;
const WINDOW_PATH = process.env.REPLAY_PERF_WINDOW;

test.skip(!DATA_PATH, 'set REPLAY_PERF_DATA to the overview.json from tools/replay_perf_record.py');

test('replay of a 100-drone, 15-minute recording: playback, scrubbing and buffer-fill cost', async ({ page }) => {
  test.setTimeout(240_000);
  const DATA = JSON.parse(fs.readFileSync(DATA_PATH, 'utf8'));
  const WINDOW = WINDOW_PATH ? JSON.parse(fs.readFileSync(WINDOW_PATH, 'utf8')) : null;
  let windowRequests = 0;
  await signIn(page);
  await page.routeWebSocket('**/ws/telemetry', () => {});
  await page.route('**/choreography.json', (r) => r.fulfill({ json: { drones: [] } }));
  await page.route('**/api/blackbox/**', (route) => {
    const p = new URL(route.request().url()).pathname;
    if (p === '/api/blackbox/sessions') {
      return route.fulfill({ json: { sessions: [{ id: 'perf', bytes: 1, modified: 1, started: '2026-11-14T00:00:00Z', context: {}, live: false }] } });
    }
    if (p.endsWith('/events')) return route.fulfill({ json: DATA.events });
    if (p.endsWith('/frames') && new URL(route.request().url()).searchParams.has('from_ms')) {
      windowRequests += 1;
      if (!WINDOW) return route.fulfill({ status: 404, json: { detail: 'no window data' } });
      const from = Number(new URL(route.request().url()).searchParams.get('from_ms'));
      const shift = from - WINDOW.t_ms_first;
      const frames = WINDOW.frames.map((f) => ({ ...f, t_ms: f.t_ms + shift }));
      return route.fulfill({ json: { ...WINDOW, t_ms_first: from, t_ms_last: from + (WINDOW.t_ms_last - WINDOW.t_ms_first), frames } });
    }
    if (p.endsWith('/frames')) return route.fulfill({ json: DATA.frames });
    return route.fulfill({ json: DATA.summary });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'REPLAY' }).click();
  const loadStart = Date.now();
  await page.getByRole('button', { name: 'LOAD' }).click();
  await page.getByTestId('replay-clock').waitFor();
  const loadMs = Date.now() - loadStart;

  const measured = await page.evaluate(async () => {
    const gl = document.createElement('canvas').getContext('webgl2') || document.createElement('canvas').getContext('webgl');
    const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
    const renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : 'unknown';
    const sample = (ms, action) => new Promise((resolve) => {
      const dts = [];
      let last = performance.now();
      const start = last;
      function tick(now) {
        dts.push(now - last);
        last = now;
        if (action) action(now - start);
        if (now - start >= ms) resolve(dts);
        else requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    });
    const stats = (dts) => {
      const s = dts.slice(2).sort((a, b) => a - b);
      const q = (p) => s[Math.min(s.length - 1, Math.floor(p * s.length))];
      return {
        frames: s.length,
        fps: +(1000 / (s.reduce((a, b) => a + b, 0) / s.length)).toFixed(1),
        p50_ms: +q(0.5).toFixed(1),
        p95_ms: +q(0.95).toFixed(1),
        p99_ms: +q(0.99).toFixed(1),
        max_ms: +s[s.length - 1].toFixed(1),
      };
    };
    const idle = stats(await sample(4000));
    const speed = document.querySelector('select[aria-label="Playback speed"]');
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(speed, '10');
    speed.dispatchEvent(new Event('change', { bubbles: true }));
    document.querySelector('button[aria-label="Play"]').click();
    const playing = stats(await sample(6000));
    // scrubbing: move the range input every animation frame, sweeping the whole timeline every 3 s
    const range = document.querySelector('input[aria-label="Timeline"]');
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    const max = Number(range.max);
    const scrub = stats(await sample(6000, (elapsed) => {
      setValue.call(range, String(Math.round(((elapsed / 3000) % 1) * max)));
      range.dispatchEvent(new Event('input', { bubbles: true }));
    }));
    return { renderer, idle, playing, scrub };
  });

  const fill = await page.evaluate(async (frames) => {
    const { fillFleetBuffer } = await import('/src/replay.js');
    const { FLOATS_PER_DRONE } = await import('/src/telemetry.js');
    const buffer = { data: new Float32Array(1000 * FLOATS_PER_DRONE), count: 0 };
    const calls = 20000;
    const end = frames[frames.length - 1].t_ms;
    const t = performance.now();
    for (let i = 0; i < calls; i++) fillFleetBuffer(frames, (i * 37) % end, buffer);
    return { drones: buffer.count, per_call_ms: +((performance.now() - t) / calls).toFixed(4) };
  }, DATA.frames.frames);

  const result = { loadMs, windowRequests, fullRateWindows: !!WINDOW, ...measured, fill };
  console.log(`REPLAY PERF ${JSON.stringify(result)}`);
  await test.info().attach('replay-perf.json', { body: JSON.stringify(result, null, 1), contentType: 'application/json' });
});
