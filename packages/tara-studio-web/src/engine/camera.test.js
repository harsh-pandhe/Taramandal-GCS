import { describe, it, expect } from 'vitest';
import {
  AUDIENCE_EYE_HEIGHT_M, MIN_RADIUS, MAX_RADIUS, MIN_PHI, MAX_PHI, PAN_LIMIT_M,
  createCamera, toScene, cameraPosition, cameraDistance, worldPerPixel,
  orbitBy, zoomBy, panBy, frameBounds, showBounds, fleetBoundsAt,
} from './camera.js';

const VIEWS = ['3d', 'top-down', 'audience'];
const FOV = 60;

const sub = (a, b) => ({ x: a.x - b.x, y: a.y - b.y, z: a.z - b.z });
const dot = (a, b) => a.x * b.x + a.y * b.y + a.z * b.z;
const cross = (a, b) => ({
  x: a.y * b.z - a.z * b.y, y: a.z * b.x - a.x * b.z, z: a.x * b.y - a.y * b.x,
});
const norm = (a) => {
  const l = Math.hypot(a.x, a.y, a.z);
  return { x: a.x / l, y: a.y / l, z: a.z / l };
};

// The screen axes three's lookAt() gives an eye looking at the target with
// world-up +Y, worked out from the eye position alone. panBy() carries its own
// closed-form version of these, so this is the independent check on it.
const screenAxes = (cam, viewMode) => {
  const back = norm(sub(cameraPosition(cam, viewMode), cam.target));
  const right = norm(cross({ x: 0, y: 1, z: 0 }, back));
  return { right, up: cross(back, right), back };
};

// Screen position of a world point, in metres at the target's depth.
const onScreen = (cam, viewMode, p) => {
  const { right, up } = screenAxes(cam, viewMode);
  const rel = sub(p, cam.target);
  return { x: dot(rel, right), y: dot(rel, up) };
};

const corners = ({ min, max }) => {
  const out = [];
  for (const x of [min.x, max.x]) for (const y of [min.y, max.y]) for (const z of [min.z, max.z]) {
    out.push({ x, y, z });
  }
  return out;
};

const show = (tracks) => ({
  drones: tracks.map((waypoints, i) => ({ id: i + 1, waypoints })),
});
const wp = (time, x, y, z) => ({ time, x, y, z, yaw: 0 });

describe('cameraPosition', () => {
  it('keeps the eye `radius` from the target in the 3D and top-down views', () => {
    const cam = { ...createCamera(), radius: 25, target: { x: 3, y: 9, z: -4 } };
    expect(cameraDistance(cam, '3d')).toBeCloseTo(25, 9);
    expect(cameraDistance(cam, 'top-down')).toBeCloseTo(25, 5);
  });

  it('looks straight down in the top-down view, with +X on screen-right', () => {
    const cam = { ...createCamera(), target: { x: 3, y: 2, z: -4 } };
    const { right, up, back } = screenAxes(cam, 'top-down');
    expect(back.y).toBeGreaterThan(0.999999);
    expect(right.x).toBeCloseTo(1, 6);
    expect(up.z).toBeCloseTo(-1, 6);
  });

  it('puts the audience at eye height on the -X side, with +Z on screen-right', () => {
    const cam = { ...createCamera(), radius: 30, target: { x: 2, y: 12, z: 7 } };
    const eye = cameraPosition(cam, 'audience');
    expect(eye).toEqual({ x: -28, y: AUDIENCE_EYE_HEIGHT_M, z: 7 });
    // +Z on screen-right is what makes a text formation read left to right
    // (engine/audienceView.test.js).
    expect(screenAxes(cam, 'audience').right.z).toBe(1);
  });

  it('measures the real audience distance, which is longer than the radius', () => {
    const cam = { ...createCamera(), radius: 30, target: { x: 0, y: 41.7, z: 0 } };
    expect(cameraDistance(cam, 'audience')).toBeCloseTo(50, 9); // 30-40-50
  });
});

describe('orbitBy', () => {
  it('rotates without moving the target or changing the distance', () => {
    const cam = createCamera();
    const next = orbitBy(cam, 120, -40);
    expect(next.target).toEqual(cam.target);
    expect(next.radius).toBe(cam.radius);
    expect(next.theta).not.toBe(cam.theta);
    expect(cameraDistance(next, '3d')).toBeCloseTo(cam.radius, 9);
  });

  it('never tips the eye below the ground or over the top', () => {
    const cam = { ...createCamera(), target: { x: 0, y: 0, z: 0 } };
    const low = orbitBy(cam, 0, -100000);
    const high = orbitBy(cam, 0, 100000);
    expect(low.phi).toBe(MAX_PHI);
    expect(high.phi).toBe(MIN_PHI);
    expect(cameraPosition(low, '3d').y).toBeGreaterThan(0);
  });

  it('returns a new camera and leaves the old one alone', () => {
    const cam = createCamera();
    const before = JSON.stringify(cam);
    orbitBy(cam, 10, 10);
    expect(JSON.stringify(cam)).toBe(before);
  });
});

describe('zoomBy', () => {
  it('moves the eye along its line of sight and keeps the target', () => {
    const cam = createCamera();
    const next = zoomBy(cam, 200);
    expect(next.radius).toBe(cam.radius + 10);
    expect(next.target).toEqual(cam.target);
  });

  it('holds the zoom limits', () => {
    expect(zoomBy(createCamera(), -1e9).radius).toBe(MIN_RADIUS);
    expect(zoomBy(createCamera(), 1e9).radius).toBe(MAX_RADIUS);
  });
});

describe('panBy', () => {
  const cam = {
    theta: 0.7, phi: 0.9, radius: 40, target: { x: 5, y: 10, z: -3 },
  };

  it.each(VIEWS)('makes the scene follow the pointer in the %s view', (viewMode) => {
    // A point that sits under the pointer must move across the screen by the
    // drag itself: right for a drag right, down for a drag down.
    const metresPerPx = 0.05;
    const next = panBy(cam, viewMode, 80, 30, metresPerPx);
    const before = onScreen(cam, viewMode, cam.target);
    const after = onScreen(next, viewMode, cam.target);
    if (viewMode === 'audience') {
      // The gaze tilts rather than the eye rising, so only the sideways walk
      // is an exact slide; the look-at point still goes up for a drag down.
      expect(after.x - before.x).toBeCloseTo(80 * metresPerPx, 9);
      expect(next.target.y - cam.target.y).toBeCloseTo(30 * metresPerPx, 9);
    } else {
      expect(after.x - before.x).toBeCloseTo(80 * metresPerPx, 4);
      expect(after.y - before.y).toBeCloseTo(-30 * metresPerPx, 4);
    }
  });

  it('keeps the angles and the eye-to-target distance in the 3D view', () => {
    const next = panBy(cam, '3d', 200, -150, 0.04);
    expect(next.target).not.toEqual(cam.target);
    expect(next.theta).toBe(cam.theta);
    expect(next.phi).toBe(cam.phi);
    expect(next.radius).toBe(cam.radius);
    expect(cameraDistance(next, '3d')).toBeCloseTo(cameraDistance(cam, '3d'), 9);
  });

  it('slides over the ground in the top-down view, keeping the height', () => {
    const next = panBy(cam, 'top-down', 100, 60, 0.1);
    expect(next.target).toEqual({ x: -5, y: 10, z: -9 });
  });

  it('walks the spectator along the audience line without leaving it', () => {
    const next = panBy(cam, 'audience', 100, 0, 0.1);
    const eye = cameraPosition(next, 'audience');
    expect(next.target).toEqual({ x: 5, y: 10, z: -13 });
    expect(eye.y).toBe(AUDIENCE_EYE_HEIGHT_M);
    expect(eye.x).toBe(cameraPosition(cam, 'audience').x);
  });

  it('never moves the target below the ground', () => {
    for (const viewMode of VIEWS) {
      expect(panBy(cam, viewMode, 0, -1e6, 1).target.y).toBeGreaterThanOrEqual(0);
      expect(panBy(cam, viewMode, 0, 1e6, 1).target.y).toBeGreaterThanOrEqual(0);
    }
  });

  it('cannot pan the site out of reach', () => {
    for (const viewMode of VIEWS) {
      const { target } = panBy(cam, viewMode, 1e7, 1e7, 1);
      for (const k of ['x', 'y', 'z']) expect(Math.abs(target[k])).toBeLessThanOrEqual(PAN_LIMIT_M);
    }
  });
});

describe('audience side', () => {
  it('stays at eye height on the -X side through any pan, zoom and fit', () => {
    let cam = createCamera();
    const bounds = { min: { x: -20, y: 0, z: -25 }, max: { x: 20, y: 45, z: 25 } };
    const steps = [
      (c) => panBy(c, 'audience', 900, -400, 0.2),
      (c) => zoomBy(c, -1e6),
      (c) => panBy(c, 'audience', -5000, 800, 0.5),
      (c) => zoomBy(c, 1e6),
      (c) => frameBounds(c, bounds, { fovDeg: FOV, aspect: 16 / 9 }),
      (c) => orbitBy(c, 700, 300),
      (c) => zoomBy(c, -1e6),
    ];
    for (const step of steps) {
      cam = step(cam);
      const eye = cameraPosition(cam, 'audience');
      expect(eye.y).toBe(AUDIENCE_EYE_HEIGHT_M);
      expect(eye.x).toBeLessThan(cam.target.x);
      expect(screenAxes(cam, 'audience').right.z).toBe(1);
    }
  });
});

describe('frameBounds', () => {
  const bounds = { min: { x: -18, y: 0, z: -6 }, max: { x: 22, y: 30, z: 14 } };

  it('centres the target on the box', () => {
    const cam = frameBounds(createCamera(), bounds, { fovDeg: FOV, aspect: 1.6 });
    expect(cam.target).toEqual({ x: 2, y: 15, z: 4 });
  });

  it.each([
    ['3d', 16 / 9], ['top-down', 16 / 9], ['audience', 16 / 9],
    ['3d', 0.6], ['top-down', 0.6], ['audience', 0.6], // a tall, narrow stage
  ])('holds every corner of the box in the %s view at aspect %f', (viewMode, aspect) => {
    const cam = frameBounds(createCamera(), bounds, { fovDeg: FOV, aspect });
    const eye = cameraPosition(cam, viewMode);
    const { right, up, back } = screenAxes(cam, viewMode);
    const tanV = Math.tan((FOV * Math.PI) / 360);
    for (const corner of corners(bounds)) {
      const rel = sub(corner, eye);
      const depth = -dot(rel, back);
      expect(depth).toBeGreaterThan(0);
      expect(Math.abs(dot(rel, up)) / depth).toBeLessThan(tanV);
      expect(Math.abs(dot(rel, right)) / depth).toBeLessThan(tanV * aspect);
    }
  });

  it('keeps the orbit angles, so a fit does not spin the view', () => {
    const start = orbitBy(createCamera(), 300, 80);
    const cam = frameBounds(start, bounds, { fovDeg: FOV, aspect: 1.6 });
    expect(cam.theta).toBe(start.theta);
    expect(cam.phi).toBe(start.phi);
  });

  it('does not zoom into a single drone or past the zoom limit', () => {
    const point = { min: { x: 1, y: 8, z: 1 }, max: { x: 1, y: 8, z: 1 } };
    expect(frameBounds(createCamera(), point).radius).toBe(12);
    const huge = { min: { x: -400, y: 0, z: -400 }, max: { x: 400, y: 100, z: 400 } };
    expect(frameBounds(createCamera(), huge).radius).toBe(MAX_RADIUS);
  });

  it('leaves the camera alone when there is nothing to frame', () => {
    const cam = createCamera();
    expect(frameBounds(cam, null)).toBe(cam);
  });
});

describe('show bounds', () => {
  // NED waypoints: z is down, so 10 m up is z = -10.
  const built = show([
    [wp(0, 0, 0, 0), wp(10, 0, 0, -10), wp(20, 8, -6, -10)],
    [wp(0, 2, 0, 0), wp(10, 2, 0, -20), wp(20, -4, 12, -20)],
  ]);

  it('maps NED into the scene frame', () => {
    expect(toScene({ x: 1, y: 2, z: -3 })).toEqual({ x: 1, y: 3, z: 2 });
  });

  it('boxes every waypoint of the whole show', () => {
    expect(showBounds(built)).toEqual({
      min: { x: -4, y: 0, z: -6 }, max: { x: 8, y: 20, z: 12 },
    });
  });

  it('boxes only where the fleet is at the given time', () => {
    expect(fleetBoundsAt(built, 0)).toEqual({
      min: { x: 0, y: 0, z: 0 }, max: { x: 2, y: 0, z: 0 },
    });
    expect(fleetBoundsAt(built, 15)).toEqual({
      min: { x: -1, y: 10, z: -3 }, max: { x: 4, y: 20, z: 6 },
    });
  });

  it('returns null for a missing or empty show', () => {
    expect(showBounds(null)).toBeNull();
    expect(showBounds(show([]))).toBeNull();
    expect(fleetBoundsAt(null, 0)).toBeNull();
    expect(fleetBoundsAt(show([[]]), 0)).toBeNull();
  });
});

describe('worldPerPixel', () => {
  it('is the visible height at that distance divided by the viewport height', () => {
    // A 90 degree lens sees 2 m of height per metre of distance.
    expect(worldPerPixel(50, 90, 500)).toBeCloseTo(0.2, 9);
  });

  it('does not divide by a zero-height viewport', () => {
    expect(Number.isFinite(worldPerPixel(50, 60, 0))).toBe(true);
  });
});
