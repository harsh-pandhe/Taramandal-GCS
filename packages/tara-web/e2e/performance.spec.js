import { expect, test } from '@playwright/test';

// Issue #23's performance criteria: >=55 FPS at 1000 instanced drones, and a
// stable memory footprint over a long run.
//
// Honest caveat, because a green tick here is easy to over-read: these numbers
// depend entirely on the GPU the suite runs on. On a CI box falling back to
// SwiftShader (software GL) the FPS assertion measures the software rasteriser,
// not the pipeline — so it is skipped rather than allowed to fail spuriously or,
// worse, to "pass" and imply a hardware result nobody measured. The criterion's
// "standard GPU hardware" is a claim only a real GPU run can support.

const HARNESS = '/e2e/harness/nightsky.html';

// Incremented by a framenavigated listener while a measurement is in flight; a
// non-zero value means the page reloaded mid-run and the numbers are void.
let navigationsDuringRun = 0;

const FLEET_SIZE = 1000;
const TARGET_FPS = 55;
const MEMORY_BUDGET_MB = 150;

// Measures over a fixed wall-clock window rather than a frame count, so a slow
// renderer can't stretch the sample window and dilute its own bad result.
async function measureFps(page, durationMs) {
  return page.evaluate(async (duration) => {
    const frames = [];
    let last = performance.now();
    const start = last;

    await new Promise((resolve) => {
      function tick(now) {
        frames.push(now - last);
        last = now;
        if (now - start >= duration) resolve();
        else requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    });

    // Drop the first few frames: they include shader compilation and the
    // initial buffer upload, which are startup costs, not steady-state ones.
    const steady = frames.slice(5);
    const mean = steady.reduce((a, b) => a + b, 0) / steady.length;
    const sorted = [...steady].sort((a, b) => a - b);
    // p95 frame time is the number that decides whether a viewport feels
    // smooth — a good mean with periodic 40ms spikes reads as stutter.
    const p95 = sorted[Math.floor(sorted.length * 0.95)];
    return { meanFps: 1000 / mean, p95Fps: 1000 / p95, frameCount: steady.length };
  }, durationMs);
}

async function usesSoftwareRenderer(page) {
  return page.evaluate(() => {
    const canvas = document.querySelector('canvas');
    const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    const info = gl.getExtension('WEBGL_debug_renderer_info');
    const renderer = info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : '';
    return /swiftshader|llvmpipe|software|microsoft basic/i.test(String(renderer));
  });
}

async function bootFleet(page, count) {
  await page.goto(HARNESS);
  await page.waitForFunction(() => window.__nightsky !== undefined);
  await expect(page.getByTestId('deviation-hud')).toBeVisible();
  // Detect the page going away underneath a long measurement. Vite's HMR client
  // reloads on any source change, which silently swaps the harness for the real
  // dashboard mid-run — a 15-minute soak is long enough that an unrelated edit
  // landing during it is a genuine hazard, and the raw failure ("execution
  // context was destroyed") looks nothing like its cause. The harness page opts
  // out of HMR; this catches a reload from any other source and says why.
  page.on('framenavigated', (frame) => {
    if (frame === page.mainFrame()) navigationsDuringRun += 1;
  });
  navigationsDuringRun = 0;
  await page.evaluate(
    ([n]) => {
      // A realistic mix rather than a uniformly nominal fleet: critical drones
      // take the more expensive path (flash factor, larger glow billboard), so
      // measuring without any of them would understate the real cost.
      const deviations = Array.from({ length: n }, (_, i) => (i % 20 === 0 ? 1.2 : 0.1));
      window.__nightsky.setFleet({ count: n, deviations });
    },
    [count],
  );
  await expect(page.getByTestId('hud-tracked')).toHaveText(`TRACKED ${count}/${count}`);
}

test.describe('1000-drone viewport performance', () => {
  test('holds >=55 FPS with 1000 instanced drones', async ({ page }) => {
    await bootFleet(page, FLEET_SIZE);

    test.skip(
      await usesSoftwareRenderer(page),
      'No hardware GPU available — a software-rasteriser FPS number says nothing about the criterion.',
    );

    const { meanFps, p95Fps, frameCount } = await measureFps(page, 5000);
    expect(frameCount).toBeGreaterThan(60);

    // eslint-disable-next-line no-console
    console.log(`1000 drones: mean ${meanFps.toFixed(1)} FPS, p95 ${p95Fps.toFixed(1)} FPS`);
    expect(meanFps).toBeGreaterThanOrEqual(TARGET_FPS);
  });

  test('draws the whole fleet in a bounded number of draw calls', async ({ page }) => {
    // The instancing claim, asserted directly rather than inferred from FPS:
    // this is what stops a future refactor from quietly reintroducing a
    // per-drone mesh that happens to still hit 60 FPS on a fast dev machine and
    // then collapses on the show laptop.
    await bootFleet(page, FLEET_SIZE);

    const calls = await page.evaluate(() => {
      const canvas = document.querySelector('canvas');
      const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
      let count = 0;
      for (const name of ['drawElements', 'drawArrays', 'drawElementsInstanced', 'drawArraysInstanced']) {
        const original = gl[name].bind(gl);
        gl[name] = (...args) => {
          count += 1;
          return original(...args);
        };
      }
      return new Promise((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve(count)));
      });
    });

    // Two frames of: 2 instanced fleet meshes + starfield + ground + the
    // bloom composer's passes. Well under the ~2000 a per-drone-mesh
    // implementation would need, while leaving room for the postprocessing
    // chain to change shape.
    expect(calls).toBeLessThan(80);
  });

  test('memory stays within budget and does not grow over a sustained run', async ({ page, browserName }) => {
    test.skip(browserName !== 'chromium', 'performance.memory is Chromium-only.');
    await bootFleet(page, FLEET_SIZE);

    // The criterion says 15 minutes. That is far too slow for the default
    // suite, so the shape of the leak check is the same but the window is
    // configurable — set NIGHTSKY_SOAK_MS=900000 for the full 15-minute run.
    const soakMs = Number(process.env.NIGHTSKY_SOAK_MS || 20_000);
    // test.setTimeout() counts from the test's start, i.e. it also has to
    // cover bootFleet() above, not just the soak below. Issue #150: on a
    // severely overloaded CI runner (the same incident as #149 — run
    // 38604187493, one worker, one spec file 16x its normal duration) this
    // test's own console.log showed the soak itself finishing fine (heap
    // flat, "51.0MB -> 51.0MB over 27s") well inside an 80000ms budget, yet
    // the test still hit "Test timeout of 80000ms exceeded" — the arithmetic
    // only works if bootFleet() itself, normally a couple of seconds, ate
    // most of the remaining budget under that overload. The old 60s buffer
    // assumed setup stays cheap; it doesn't under this failure mode.
    test.setTimeout(soakMs + 180_000);

    const heapMb = () =>
      page.evaluate(() => {
        if (!performance.memory) return null;
        return performance.memory.usedJSHeapSize / (1024 * 1024);
      });

    // Let the scene settle before taking the baseline, so one-time allocations
    // aren't counted as growth.
    await page.waitForTimeout(3000);
    const before = await heapMb();
    const soakStartedAt = Date.now();
    test.skip(before === null, 'performance.memory unavailable in this browser build.');

    // Keep telemetry churning for the whole window — a static scene would not
    // exercise the per-frame path where a leak would actually accumulate.
    //
    // Driven by setInterval at the telemetry rate rather than by
    // requestAnimationFrame: rAF is throttled hard when the page isn't visible
    // (headless, backgrounded tab), so a rAF-driven loop asked for 15 minutes of
    // work takes very much longer than 15 minutes of wall clock and blows the
    // test timeout instead of finishing. setInterval keeps the soak's real
    // duration equal to the duration it was asked for. This only paces the
    // telemetry feed; the renderer still runs on its own rAF, which is the
    // thing under test.
    await page.evaluate((duration) => {
      const start = performance.now();
      const FEED_HZ = 20; // matches tara_gcs.telemetry_codec.FAST_HZ
      return new Promise((resolve) => {
        const timer = setInterval(() => {
          const t = (performance.now() - start) / 1000;
          const deviations = Array.from({ length: 1000 }, (_, i) =>
            i % 20 === 0 ? 1.2 : 0.1 + 0.2 * Math.sin(t + i * 0.01),
          );
          window.__nightsky.setFleet({ count: 1000, deviations });
          if (performance.now() - start >= duration) {
            clearInterval(timer);
            resolve();
          }
        }, 1000 / FEED_HZ);
      });
    }, soakMs);

    expect(
      navigationsDuringRun,
      'the page reloaded mid-soak (Vite HMR?), so the heap numbers are not comparable',
    ).toBe(0);

    // A soak that quietly ran for a fraction of its window would report a
    // reassuring flat heap while having tested almost nothing, so assert the
    // elapsed wall clock rather than trusting the loop to have paced itself.
    const soakElapsedMs = Date.now() - soakStartedAt;
    expect(soakElapsedMs).toBeGreaterThanOrEqual(soakMs * 0.9);

    const after = await heapMb();
    // eslint-disable-next-line no-console
    console.log(
      `heap: ${before.toFixed(1)}MB -> ${after.toFixed(1)}MB over ${(soakElapsedMs / 1000).toFixed(0)}s`,
    );

    expect(after).toBeLessThan(MEMORY_BUDGET_MB);
    // Growth, not just the absolute ceiling: a slow leak can stay under budget
    // for a 20s sample and still exhaust it across a real show.
    expect(after - before).toBeLessThan(25);
  });
});
