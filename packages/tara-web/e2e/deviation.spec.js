import zlib from 'node:zlib';

import { expect, test } from '@playwright/test';

// Issue #23's core acceptance criterion: a drone's colour must shift from green
// to red when its position deviates more than 0.8m from plan.
//
// Two layers of assertion, deliberately:
//   1. Sampled canvas pixels — the actual rendered colour, which is what the
//      criterion is about. A unit test of the threshold function would pass
//      even if the colour never reached the GPU.
//   2. The HUD's band counts — cheap, exact, and they localise a failure to the
//      classification rather than the rendering when both break at once.

const HARNESS = '/e2e/harness/nightsky.html';

async function bootHarness(page) {
  await page.goto(HARNESS);
  await page.waitForFunction(() => window.__nightsky !== undefined);
  // The HUD only appears once the render loop has produced its first summary.
  await expect(page.getByTestId('deviation-hud')).toBeVisible();
}

// Drives one fleet configuration and waits for it to reach the screen. The HUD
// summary is pushed from inside the render loop every 250ms, so waiting on the
// count to settle is what guarantees the canvas we then sample is the frame
// that corresponds to the fleet we just set.
async function applyFleet(page, config, expected) {
  await page.evaluate((cfg) => window.__nightsky.setFleet(cfg), config);
  for (const [band, count] of Object.entries(expected)) {
    await expect(page.getByTestId(`hud-${band}`)).toHaveText(String(count));
  }
}

// Reports the average colour of everything lit in the frame as {r,g,b}.
//
// Averaging the lit region rather than sampling the single brightest pixel is
// deliberate: at the centre of a bloomed LED every channel clips to ~255, so
// the brightest pixel is near-white regardless of band and carries almost no
// hue. The surrounding glow is where the colour actually lives. Since the fleet
// is the only lit thing in a black sky, the mean over lit pixels is the fleet's
// colour.
//
// This goes through a screenshot rather than gl.readPixels: the renderer draws
// to a back buffer that is presented and cleared each frame, so reading the
// live context from outside the render loop returns an all-black buffer
// (preserveDrawingBuffer would fix that, but only by making the component under
// test slower than the one that ships). A screenshot captures the composited
// frame, which is what a director would actually see.
async function fleetColor(page) {
  const png = await page.locator('canvas').screenshot();
  return meanLitColor(png);
}

// Minimal PNG reader: the screenshot is a truecolour PNG, so inflating IDAT and
// undoing the per-scanline filters is enough to walk the pixels. Pulling in an
// image library for one assertion would be a heavier dependency than the test
// deserves.
function meanLitColor(png) {
  const chunks = [];
  let offset = 8; // skip the PNG signature
  let width = 0;
  let height = 0;
  let colorType = 6;

  while (offset < png.length) {
    const length = png.readUInt32BE(offset);
    const type = png.toString('ascii', offset + 4, offset + 8);
    const data = png.subarray(offset + 8, offset + 8 + length);
    if (type === 'IHDR') {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      colorType = data[9];
    } else if (type === 'IDAT') {
      chunks.push(data);
    } else if (type === 'IEND') {
      break;
    }
    offset += 12 + length; // length + type + data + crc
  }

  const channels = colorType === 6 ? 4 : 3;
  const raw = zlib.inflateSync(Buffer.concat(chunks));
  const stride = width * channels;

  // Undo PNG scanline filtering in place, one row at a time.
  const pixels = Buffer.alloc(height * stride);
  for (let y = 0; y < height; y++) {
    const filter = raw[y * (stride + 1)];
    const src = raw.subarray(y * (stride + 1) + 1, y * (stride + 1) + 1 + stride);
    const row = pixels.subarray(y * stride, (y + 1) * stride);
    const prev = y > 0 ? pixels.subarray((y - 1) * stride, y * stride) : null;

    for (let x = 0; x < stride; x++) {
      const a = x >= channels ? row[x - channels] : 0;
      const b = prev ? prev[x] : 0;
      const c = prev && x >= channels ? prev[x - channels] : 0;
      let value = src[x];
      if (filter === 1) value += a;
      else if (filter === 2) value += b;
      else if (filter === 3) value += (a + b) >> 1;
      else if (filter === 4) {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        value += pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
      }
      row[x] = value & 0xff;
    }
  }

  // "Lit" excludes the near-black sky and the dark ground silhouette, which
  // would otherwise dominate the mean and flatten the signal to nothing.
  const LIT_THRESHOLD = 24;
  let r = 0;
  let g = 0;
  let b = 0;
  let lit = 0;
  for (let i = 0; i < pixels.length; i += channels) {
    if (pixels[i] + pixels[i + 1] + pixels[i + 2] <= LIT_THRESHOLD * 3) continue;
    r += pixels[i];
    g += pixels[i + 1];
    b += pixels[i + 2];
    lit += 1;
  }
  if (lit === 0) return { r: 0, g: 0, b: 0, lit: 0 };
  return { r: r / lit, g: g / lit, b: b / lit, lit };
}

test.describe('trajectory deviation colour coding', () => {
  test.beforeEach(async ({ page }) => {
    await bootHarness(page);
  });

  test('a nominal fleet renders green and reports no alert', async ({ page }) => {
    // 0.1m off plan — comfortably inside the < 0.4m nominal band.
    await applyFleet(
      page,
      { count: 24, deviations: Array(24).fill(0.1) },
      { nominal: 24, warning: 0, critical: 0 },
    );

    const color = await fleetColor(page);
    expect(color.lit).toBeGreaterThan(0);
    expect(color.g).toBeGreaterThan(color.r);
    expect(color.g).toBeGreaterThan(color.b);

    await expect(page.getByTestId('hud-critical-alert')).toHaveCount(0);
  });

  test('a drone deviating past 0.8m turns red and raises the critical alert', async ({ page }) => {
    // This is the criterion verbatim: same fleet, same geometry, only the
    // deviation changes — so a colour change here can only come from the
    // deviation mapping.
    await applyFleet(
      page,
      { count: 24, deviations: Array(24).fill(0.1) },
      { nominal: 24, critical: 0 },
    );
    const nominal = await fleetColor(page);
    expect(nominal.lit).toBeGreaterThan(0);
    expect(nominal.g).toBeGreaterThan(nominal.r);

    await applyFleet(
      page,
      { count: 24, deviations: Array(24).fill(0.9) },
      { nominal: 0, warning: 0, critical: 24 },
    );
    const critical = await fleetColor(page);

    expect(critical.lit).toBeGreaterThan(0);
    expect(critical.r).toBeGreaterThan(critical.g);
    expect(critical.r).toBeGreaterThan(critical.b);
    // And the shift is unambiguous, not a marginal channel reordering: the
    // dominant channel swaps outright between the two states.
    expect(critical.r).toBeGreaterThan(nominal.r);
    expect(critical.g).toBeLessThan(nominal.g);
    await expect(page.getByTestId('hud-critical-alert')).toBeVisible();
    await expect(page.getByTestId('hud-critical-alert')).toContainText('OFF TRAJECTORY');
  });

  test('the 0.4m and 0.8m band edges classify per spec', async ({ page }) => {
    // The spec's bands are e < 0.4 nominal, 0.4 <= e <= 0.8 warning, e > 0.8
    // critical. These are the cases a flipped comparison operator would get
    // wrong.
    //
    // The offsets are nudged just clear of the exact boundaries rather than
    // sitting on them, because positions cross the wire as float32: a planned
    // altitude of 18m plus exactly 0.4 round-trips as 18.399999, so a drone
    // "exactly on" a boundary genuinely lands on whichever side float32
    // rounding puts it. Asserting the razor's edge would be testing IEEE-754,
    // not the classifier — a metre-scale HUD has no meaningful behaviour at
    // sub-micron distances from a threshold.
    await applyFleet(
      page,
      { count: 4, deviations: [0.39, 0.41, 0.79, 0.81] },
      { nominal: 1, warning: 2, critical: 1 },
    );
  });

  test('all three bands render simultaneously', async ({ page }) => {
    await applyFleet(
      page,
      { count: 30, deviations: [...Array(10).fill(0.1), ...Array(10).fill(0.6), ...Array(10).fill(1.5)] },
      { nominal: 10, warning: 10, critical: 10 },
    );

    await expect(page.getByTestId('hud-critical-alert')).toContainText('10 DRONES');
    await expect(page.getByTestId('hud-tracked')).toHaveText('TRACKED 30/30');
  });

  test('a fleet with no active trajectory reads as unmonitored, not nominal', async ({ page }) => {
    // The failure this guards against is the important one: if planned_valid
    // were ignored, a parked fleet would score a perfect 0.0m deviation against
    // a planned position of (0,0,0) and light up entirely green — telling a
    // director every drone is on plan when nothing is being checked at all.
    await applyFleet(page, { count: 12, deviations: Array(12).fill(0.1) }, { nominal: 12 });

    await page.evaluate(() => window.__nightsky.clearPlan());
    await expect(page.getByTestId('hud-nominal')).toHaveText('0');
    await expect(page.getByTestId('hud-tracked')).toHaveText('TRACKED 0/12');
  });

  test('the worst-offender readout tracks the largest deviation', async ({ page }) => {
    await applyFleet(
      page,
      { count: 5, deviations: [0.1, 0.2, 2.5, 0.3, 0.4] },
      { critical: 1 },
    );
    // Drone index 2 is 2.5m off — the HUD must name it, since "something is
    // wrong somewhere in 1000 drones" is not actionable on its own.
    await expect(page.getByTestId('hud-worst')).toContainText('2.50m');
    await expect(page.getByTestId('hud-worst')).toContainText('D2');
  });
});

// NightSkyView's Canvas repaints every animation frame by design (issue #23:
// OrbitControls damping and live telemetry need to land on screen with no
// explicit invalidate() call from outside the component) — correct for the
// dashboard, but it means a snapshot taken mid-loop is racing the next paint.
// Under GitHub Actions' software-rendered GL stack specifically (reproduced
// there, not locally: 2/2 CI runs failed this way, 12+ local runs across
// plain and CPU-throttled containers all passed), the multi-pass float bloom
// pipeline (see NightSkyView's EffectComposer/Bloom) does not repaint
// bit-identically frame to frame, so Playwright's pre-comparison stability
// wait ("waiting for element to be stable") never converges and times out
// before any pixel comparison runs — not a baseline mismatch, and not
// something a longer timeout fixes, since a loop that never repeats exactly
// doesn't converge given more time either.
//
// Freezing the render loop for the capture is what these two tests actually
// want: they assert a single settled frame's colours, never animation, and
// the one genuinely time-dependent effect in this view (the critical flash)
// is already excluded by using only nominal/warning fleets. See `freeze` in
// e2e/harness/nightsky.jsx.
async function freezeForCapture(page) {
  await page.evaluate(() => window.__nightsky.freeze());
  // frameloop='demand' takes effect from the next React commit, not
  // synchronously, so the in-flight 'always' loop may still owe one more
  // paint after this call returns. Two frames is enough for that paint to
  // land and for `demand` mode's own no-op frame (nothing invalidates after
  // it) to confirm nothing further changes.
  await page.evaluate(
    () => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))),
  );
}

test.describe('visual regression', () => {
  test.beforeEach(async ({ page }) => {
    await bootHarness(page);
  });

  test('nominal fleet snapshot', async ({ page }) => {
    await applyFleet(
      page,
      { count: 64, deviations: Array(64).fill(0.05) },
      { nominal: 64, critical: 0 },
    );
    // The flash phase of critical drones is time-dependent, so snapshots are
    // only taken of states with no critical drones in them; the green-to-red
    // shift itself is asserted on sampled pixels above, which is stable.
    await freezeForCapture(page);
    await expect(page.locator('canvas')).toHaveScreenshot('fleet-nominal.png');
  });

  test('warning fleet snapshot', async ({ page }) => {
    await applyFleet(
      page,
      { count: 64, deviations: Array(64).fill(0.6) },
      { warning: 64, critical: 0 },
    );
    await freezeForCapture(page);
    await expect(page.locator('canvas')).toHaveScreenshot('fleet-warning.png');
  });
});
