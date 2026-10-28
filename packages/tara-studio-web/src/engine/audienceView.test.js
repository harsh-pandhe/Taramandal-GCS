import { describe, it, expect } from 'vitest';
import { buildFormation } from './formations.js';

// Pins the coordinate derivation behind StageViewport3D's 'audience' camera
// preset, without needing a WebGL context: a text formation is a wall in
// this scene's X=0 plane (see formations.js fmtText), with the glyph's own
// left-to-right reading axis mapped to fleet y -> the scene's Z axis (see
// StageViewport3D's `dummy.position.set(wp.x, -wp.z, wp.y)`). A camera
// looking down -X has screen-right along -Z (mirrored reading); one looking
// down +X (standing on the -X side, per audience_screenX below) has
// screen-right along +Z (correct reading order). This test works that out
// algebraically for a real two-character string, rather than trusting the
// derivation by eye.

const toScene = (fleetPoint) => ({
  x: fleetPoint.x,
  y: -fleetPoint.z,
  z: fleetPoint.y,
});

// Screen-right for a camera standing on the -X side, looking toward +X,
// with world-up = +Y: right = normalize(cross(worldUp, backward)) where
// backward = normalize(eye - target) = -X direction (eye is behind target
// along -X) -> cross((0,1,0), (-1,0,0)) = (0, 0, 1). Screen-right is +Z.
const audienceScreenX = (scenePoint) => scenePoint.z;

// The mirrored case (camera on the +X side, screen-right along -Z), for
// contrast — this is the wrong side, kept here only to assert against.
const mirroredScreenX = (scenePoint) => -scenePoint.z;

describe('audience camera side (mirroring fix)', () => {
  it('reads a two-character string left-to-right from the -X side', () => {
    // '1' should read before '2' - its points should sit to the left
    // (smaller screen-x) of '2's on average, when read from the correct side.
    const pts = buildFormation('text', '12', 20);
    const mid = Math.floor(pts.length / 2);
    const first = pts.slice(0, mid).map(toScene);
    const second = pts.slice(mid).map(toScene);

    const avg = (arr, f) => arr.reduce((s, p) => s + f(p), 0) / arr.length;
    expect(avg(first, audienceScreenX)).toBeLessThan(avg(second, audienceScreenX));
  });

  it('would read backwards if the camera stood on the +X side instead', () => {
    // The regression this guards: picking the wrong orbit side.
    const pts = buildFormation('text', '12', 20);
    const mid = Math.floor(pts.length / 2);
    const first = pts.slice(0, mid).map(toScene);
    const second = pts.slice(mid).map(toScene);

    const avg = (arr, f) => arr.reduce((s, p) => s + f(p), 0) / arr.length;
    expect(avg(first, mirroredScreenX)).toBeGreaterThan(avg(second, mirroredScreenX));
  });

  it('text formations stand in a single constant-X plane, confirming a single audience side exists', () => {
    // The mirroring derivation above depends only on which direction the
    // camera looks, not on the wall's exact X offset — but it does depend
    // on the wall being a flat plane perpendicular to X in the first place.
    // 40 drones: at 60 the spacing guarantee now refuses TARA (see formations.js).
    const pts = buildFormation('text', 'TARA', 40);
    const xs = new Set(pts.map((p) => toScene(p).x.toFixed(6)));
    expect(xs.size).toBe(1);
  });
});
