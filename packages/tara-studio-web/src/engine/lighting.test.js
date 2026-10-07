import { describe, expect, it } from 'vitest';
import { buildColoredChoreo } from './lighting.js';

const choreo = {
  drones: [0, 1].map((id) => ({
    id,
    waypoints: [0, 0.5, 1, 2].map((time) => ({ time, x: id * 3, y: 0, z: -2, yaw: 0 })),
  })),
};

describe('buildColoredChoreo', () => {
  it('keeps the brightened beat pulse inside the 0-255 LED range (issue #55)', () => {
    // 'pulse' scales the colour by up to 1.6, so white goes past 1.0 - which
    // used to bake r/g/b near 400 and get the export refused by the schema.
    const baked = buildColoredChoreo(choreo, {
      effect: 'pulse', baseColor: '#ffffff', numDrones: 2, audioVolume: 1,
    });
    const channels = baked.drones.flatMap((d) => d.waypoints.flatMap((wp) => [wp.r, wp.g, wp.b]));
    expect(channels.every((v) => Number.isInteger(v) && v >= 0 && v <= 255)).toBe(true);
    expect(Math.max(...channels)).toBe(255);
  });

  it('leaves in-range colours unchanged', () => {
    const baked = buildColoredChoreo(choreo, { effect: 'solid', baseColor: '#000000', numDrones: 2 });
    expect(baked.drones[0].waypoints[0]).toMatchObject({ r: 0, g: 0, b: 0 });
  });
});
