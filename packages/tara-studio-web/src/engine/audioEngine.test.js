import { describe, it, expect, vi } from 'vitest';
import { computePeaks, drawWaveform } from './audioEngine.js';

/** Minimal AudioBuffer stand-in — computePeaks only needs this surface. */
const fakeBuffer = (channels) => ({
  numberOfChannels: channels.length,
  length: channels[0].length,
  sampleRate: 48000,
  duration: channels[0].length / 48000,
  getChannelData: (i) => channels[i],
});

const sine = (n, cycles = 1) =>
  Float32Array.from({ length: n }, (_, i) => Math.sin((2 * Math.PI * cycles * i) / n));

describe('computePeaks', () => {
  it('returns the requested bucket count', () => {
    const peaks = computePeaks(fakeBuffer([sine(4800)]), 100);
    expect(peaks.count).toBe(100);
    expect(peaks.min).toHaveLength(100);
    expect(peaks.max).toHaveLength(100);
  });

  it('captures both halves of the waveform', () => {
    // A single magnitude would draw a half-wave; min must go negative.
    const peaks = computePeaks(fakeBuffer([sine(4800)]), 50);
    expect(Math.min(...peaks.min)).toBeLessThan(-0.9);
    expect(Math.max(...peaks.max)).toBeGreaterThan(0.9);
  });

  it('tracks amplitude across the track', () => {
    // Quiet first half, loud second half.
    const n = 4000;
    const data = Float32Array.from({ length: n }, (_, i) => (i < n / 2 ? 0.1 : 1.0));
    const peaks = computePeaks(fakeBuffer([data]), 10);
    expect(peaks.max[0]).toBeCloseTo(0.1, 2);
    expect(peaks.max[9]).toBeCloseTo(1.0, 2);
  });

  it('averages channels to mono', () => {
    const n = 1000;
    const left = new Float32Array(n).fill(1.0);
    const right = new Float32Array(n).fill(0.0);
    const peaks = computePeaks(fakeBuffer([left, right]), 4);
    expect(peaks.max[0]).toBeCloseTo(0.5, 5);
  });

  it('reports silence as zero rather than NaN', () => {
    const peaks = computePeaks(fakeBuffer([new Float32Array(1000)]), 8);
    expect([...peaks.min].every((v) => v === 0)).toBe(true);
    expect([...peaks.max].every((v) => v === 0)).toBe(true);
  });

  it('survives a bucket count larger than the sample count', () => {
    // Empty buckets must not produce NaN — they'd blow up the canvas path.
    const peaks = computePeaks(fakeBuffer([sine(10)]), 100);
    expect([...peaks.max].every(Number.isFinite)).toBe(true);
    expect([...peaks.min].every(Number.isFinite)).toBe(true);
  });
});

/** Records fillRect calls so we can assert on what got painted. */
const stubCanvas = (width = 200, height = 60) => {
  const rects = [];
  const ctx = {
    setTransform: vi.fn(), clearRect: vi.fn(), beginPath: vi.fn(),
    moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(),
    fillRect: (x, y, w, h) => rects.push({ x, y, w, h }),
  };
  return {
    rects,
    canvas: {
      clientWidth: width, clientHeight: height, width: 0, height: 0,
      getContext: () => ctx,
    },
  };
};

describe('drawWaveform zoom', () => {
  const peaks = computePeaks(fakeBuffer([sine(48000, 8)]), 2000);

  it('paints one column per pixel of canvas width', () => {
    const { canvas, rects } = stubCanvas(200, 60);
    drawWaveform(canvas, peaks, { duration: 10, startTime: 0, endTime: 10 });
    expect(rects).toHaveLength(200);
  });

  it.each([
    ['1x', 0, 10],
    ['2x', 0, 5],
    ['5x', 0, 2],
  ])('fills the full canvas width at %s zoom', (_label, start, end) => {
    const { canvas, rects } = stubCanvas(200, 60);
    drawWaveform(canvas, peaks, { duration: 10, startTime: start, endTime: end });
    // The visible window always spans the canvas — a zoomed view must not
    // draw into a fraction of it and leave the rest blank.
    expect(rects[0].x).toBe(0);
    expect(rects[rects.length - 1].x).toBe(199);
  });

  it('scales the waveform to the zoomed window, not the whole track', () => {
    // Amplitude ramps 0 -> 1 across the track, so zooming into the first fifth
    // must show only quiet content.
    const n = 48000;
    const ramp = Float32Array.from({ length: n }, (_, i) => i / n);
    const ramped = computePeaks(fakeBuffer([ramp]), 2000);

    const full = stubCanvas(100, 60);
    drawWaveform(full.canvas, ramped, { duration: 10, startTime: 0, endTime: 10 });
    const zoomed = stubCanvas(100, 60);
    drawWaveform(zoomed.canvas, ramped, { duration: 10, startTime: 0, endTime: 2 });

    // Taller bar = louder. The zoomed view shows only the quiet head, so its
    // last column must be shorter than the full view's last column.
    const lastOf = (s) => s.rects[s.rects.length - 1].h;
    expect(lastOf(zoomed)).toBeLessThan(lastOf(full));
  });

  it('sizes the backing store for the device pixel ratio', () => {
    const { canvas } = stubCanvas(200, 60);
    drawWaveform(canvas, peaks, {
      duration: 10, startTime: 0, endTime: 10, devicePixelRatio: 2,
    });
    expect(canvas.width).toBe(400);
    expect(canvas.height).toBe(120);
  });

  it('falls back to a ratio of 1 when none can be detected', () => {
    const { canvas } = stubCanvas(200, 60);
    drawWaveform(canvas, peaks, { duration: 10, startTime: 0, endTime: 10 });
    expect(canvas.width).toBe(200);
    expect(canvas.height).toBe(60);
  });

  it('is a no-op without a canvas or peaks', () => {
    expect(() => drawWaveform(null, peaks, { duration: 10 })).not.toThrow();
    const { canvas } = stubCanvas();
    expect(() => drawWaveform(canvas, null, { duration: 10 })).not.toThrow();
  });

  it('does not divide by zero on a zero-length window', () => {
    const { canvas, rects } = stubCanvas(50, 60);
    drawWaveform(canvas, peaks, { duration: 10, startTime: 3, endTime: 3 });
    expect(rects.every((r) => Number.isFinite(r.y) && Number.isFinite(r.h))).toBe(true);
  });
});
