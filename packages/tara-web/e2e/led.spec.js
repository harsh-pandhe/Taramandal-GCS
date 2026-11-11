import zlib from 'node:zlib';

import { expect, test } from '@playwright/test';

// The show's own LED colours must reach the Night Sky: what Studio plays (colours, blinks, drones
// that are switched off) is what the dashboard draws, and the deviation colours stay one click away.
//
// Asserted on sampled canvas pixels, like deviation.spec.js: a unit test of the decode would pass
// even if the colour never reached the GPU.

const HARNESS = '/e2e/harness/nightsky.html';

async function bootHarness(page) {
  await page.goto(HARNESS);
  await page.waitForFunction(() => window.__nightsky !== undefined);
  await expect(page.getByTestId('deviation-hud')).toBeVisible();
}

async function fleetColor(page) {
  return meanLitColor(await page.locator('canvas').screenshot());
}

// Same minimal PNG walk as deviation.spec.js (no image library for one assertion).
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

const LED_FLEET = (colour, n = 24) => ({ count: n, led: Array(n).fill(colour) });

test.describe('show LED colours on the night sky', () => {
  test.beforeEach(async ({ page }) => {
    await bootHarness(page);
  });

  test('drones draw in the colour the show commands, not the deviation green', async ({ page }) => {
    await page.evaluate((cfg) => window.__nightsky.setFleet(cfg), LED_FLEET([255, 20, 20]));
    await expect(page.getByTestId('hud-nominal')).toHaveText('24'); // perfectly on plan
    await expect(page.getByTestId('hud-color-mode')).toBeVisible();
    const red = await fleetColor(page);
    expect(red.lit).toBeGreaterThan(0);
    expect(red.r).toBeGreaterThan(red.g * 1.4);
    expect(red.r).toBeGreaterThan(red.b * 1.4);

    // A colour change in the show reaches the screen (blue now), still on plan.
    await page.evaluate((cfg) => window.__nightsky.setFleet(cfg), LED_FLEET([20, 40, 255]));
    // A canvas screenshot under software GL takes seconds on a CI runner, longer than the 5 s default of
    // expect.poll, so it never got a result; give it the same allowance as the empty-sky poll below.
    await expect.poll(async () => (await fleetColor(page)).b, { timeout: 30_000 }).toBeGreaterThan(80);
    const blue = await fleetColor(page);
    expect(blue.b).toBeGreaterThan(blue.r * 1.4);
    expect(blue.b).toBeGreaterThan(blue.g);
  });

  test('a drone whose LED is off goes dark instead of drawing a dim colour', async ({ page }) => {
    // The starfield also counts as lit, so measure against the empty sky.
    await page.evaluate((cfg) => window.__nightsky.setFleet(cfg), { count: 0 });
    await expect.poll(async () => (await fleetColor(page)).lit, { timeout: 30_000 }).toBeGreaterThan(0);
    const sky = await fleetColor(page);

    await page.evaluate((cfg) => window.__nightsky.setFleet(cfg), LED_FLEET([255, 255, 255]));
    await expect.poll(async () => (await fleetColor(page)).lit, { timeout: 30_000 }).toBeGreaterThan(sky.lit * 2);
    const on = await fleetColor(page);

    await page.evaluate((cfg) => window.__nightsky.setFleet(cfg), LED_FLEET([0, 0, 0]));
    // back to (almost) the empty sky: the off drones add next to no lit pixels
    await expect
      .poll(async () => (await fleetColor(page)).lit, { timeout: 60_000 })
      .toBeLessThan(sky.lit + (on.lit - sky.lit) * 0.1);
  });

  test('the DEVIATION switch returns to the deviation colours and back', async ({ page }) => {
    await page.evaluate((cfg) => window.__nightsky.setFleet(cfg), LED_FLEET([255, 20, 20]));
    await expect(page.getByTestId('hud-mode-led')).toHaveAttribute('aria-pressed', 'true');
    await page.getByTestId('hud-mode-deviation').dispatchEvent('click');
    await expect(page.getByTestId('hud-mode-deviation')).toHaveAttribute('aria-pressed', 'true');
    await expect.poll(async () => { const c = await fleetColor(page); return c.g > c.r; }, { timeout: 60_000 }).toBe(true);

    await page.getByTestId('hud-mode-led').dispatchEvent('click');
    await expect.poll(async () => { const c = await fleetColor(page); return c.r > c.g * 1.4; }, { timeout: 60_000 }).toBe(true);
  });

  test('a drone far off its trajectory keeps its alarm in LED mode', async ({ page }) => {
    await page.evaluate((cfg) => window.__nightsky.setFleet(cfg), { ...LED_FLEET([255, 255, 255], 8), deviations: Array(8).fill(2.5) });
    await expect(page.getByTestId('hud-critical')).toHaveText('8');
    await expect(page.getByTestId('hud-critical-alert')).toBeVisible();
  });

  test('a show without colour data keeps the deviation colours and shows no colour switch', async ({ page }) => {
    await page.evaluate((cfg) => window.__nightsky.setFleet(cfg), { count: 24, deviations: Array(24).fill(0.1) });
    await expect(page.getByTestId('hud-nominal')).toHaveText('24');
    await expect(page.getByTestId('hud-color-mode')).toHaveCount(0);
    const c = await fleetColor(page);
    expect(c.g).toBeGreaterThan(c.r);
    expect(c.g).toBeGreaterThan(c.b);
  });
});
