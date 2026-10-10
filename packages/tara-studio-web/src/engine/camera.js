// Camera maths for the stage preview (StageViewport3D).
//
// Kept free of three.js and the DOM so it runs under vitest: the component
// only wires pointer events to these functions and copies the result onto its
// three camera. Everything here is in the preview's scene frame (x north,
// y up, z east), and every function returns a new camera rather than mutating.
//
// A camera is { theta, phi, radius, target }: an orbit of `radius` metres
// around `target`, shared by all three views so the point being inspected
// stays put when switching between them.

import { interpolateWaypoint } from './sequencer.js';

export const AUDIENCE_EYE_HEIGHT_M = 1.7; // a spectator standing on the ground
export const MIN_RADIUS = 4;
export const MAX_RADIUS = 400;
export const MIN_PHI = 0.05;
export const MAX_PHI = Math.PI / 2.05; // stay above ground
// The scene is a 30 m site; this only stops a runaway pan from losing it.
export const PAN_LIMIT_M = 500;

const ORBIT_RAD_PER_PX = 0.005;
const ZOOM_M_PER_WHEEL_UNIT = 0.05;
// Looking straight down leaves the camera's roll undefined, so the top-down
// eye sits a hair off the vertical to keep +X on screen-right.
const TOP_DOWN_NUDGE = 0.001;
const FRAME_MIN_RADIUS = 12;
const FRAME_MARGIN = 1.1;

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export const createCamera = () => ({
  theta: Math.PI / 4, phi: Math.PI / 3, radius: 40, target: { x: 5, y: 4, z: 5 },
});

/** NED waypoint (x north, y east, z down) to the scene frame. */
export const toScene = (wp) => ({ x: wp.x, y: 0 - wp.z, z: wp.y }); // 0 - z: ground is 0, not -0

/** Where the eye sits for a camera in the given view. */
export const cameraPosition = (cam, viewMode) => {
  const { target: t, radius: r } = cam;
  if (viewMode === 'top-down') {
    return { x: t.x, y: t.y + r, z: t.z + TOP_DOWN_NUDGE };
  }
  if (viewMode === 'audience') {
    // Ground level on the -X side, looking toward +X. The side is not
    // arbitrary: a text formation is a wall in the X=0 plane whose reading
    // axis runs along +Z, and only a camera looking down +X has +Z on
    // screen-right. From the +X side the text reads mirrored. `radius` never
    // drops below MIN_RADIUS, so no pan or zoom can cross to the other side.
    // See engine/audienceView.test.js for the derivation.
    return { x: t.x - r, y: AUDIENCE_EYE_HEIGHT_M, z: t.z };
  }
  return {
    x: t.x + r * Math.sin(cam.phi) * Math.sin(cam.theta),
    y: t.y + r * Math.cos(cam.phi),
    z: t.z + r * Math.sin(cam.phi) * Math.cos(cam.theta),
  };
};

/** Real eye-to-target distance, which is not `radius` in the audience view. */
export const cameraDistance = (cam, viewMode) => {
  const p = cameraPosition(cam, viewMode);
  return Math.hypot(p.x - cam.target.x, p.y - cam.target.y, p.z - cam.target.z);
};

/** World metres covered by one screen pixel at `distance` from the eye. */
export const worldPerPixel = (distance, fovDeg, viewportHeightPx) => (
  (2 * distance * Math.tan((fovDeg * Math.PI) / 360)) / (viewportHeightPx || 1)
);

/** Rotate the 3D view by a pointer drag of (dxPx, dyPx). */
export const orbitBy = (cam, dxPx, dyPx) => ({
  ...cam,
  theta: cam.theta - dxPx * ORBIT_RAD_PER_PX,
  phi: clamp(cam.phi - dyPx * ORBIT_RAD_PER_PX, MIN_PHI, MAX_PHI),
});

/** Move the eye toward or away from the target by a wheel delta. */
export const zoomBy = (cam, wheelDeltaY) => ({
  ...cam,
  radius: clamp(cam.radius + wheelDeltaY * ZOOM_M_PER_WHEEL_UNIT, MIN_RADIUS, MAX_RADIUS),
});

const clampTarget = (t) => ({
  x: clamp(t.x, -PAN_LIMIT_M, PAN_LIMIT_M),
  y: clamp(t.y, 0, PAN_LIMIT_M), // never look at a point below ground
  z: clamp(t.z, -PAN_LIMIT_M, PAN_LIMIT_M),
});

/**
 * Move the target by a pointer drag of (dxPx, dyPx), so the scene follows the
 * pointer. `metresPerPx` is worldPerPixel() at the current distance.
 *
 *   3d        the target slides in the camera's own screen plane
 *   top-down  the target slides over the ground, its height unchanged
 *   audience  the spectator walks along the audience line (z) and raises or
 *             lowers their gaze (target height); the eye stays where
 *             cameraPosition() puts it
 */
export const panBy = (cam, viewMode, dxPx, dyPx, metresPerPx) => {
  const dx = dxPx * metresPerPx, dy = dyPx * metresPerPx;
  const t = cam.target;
  if (viewMode === 'audience') {
    return { ...cam, target: clampTarget({ x: t.x, y: t.y + dy, z: t.z - dx }) };
  }
  if (viewMode === 'top-down') {
    // Screen-right is +X and screen-up is -Z (see TOP_DOWN_NUDGE).
    return { ...cam, target: clampTarget({ x: t.x - dx, y: t.y, z: t.z - dy }) };
  }
  // Screen-right and screen-up for an eye at (theta, phi) on the orbit.
  const st = Math.sin(cam.theta), ct = Math.cos(cam.theta);
  const sp = Math.sin(cam.phi), cp = Math.cos(cam.phi);
  const right = { x: ct, y: 0, z: -st };
  const up = { x: -cp * st, y: sp, z: -cp * ct };
  return {
    ...cam,
    target: clampTarget({
      x: t.x - dx * right.x + dy * up.x,
      y: t.y - dx * right.y + dy * up.y,
      z: t.z - dx * right.z + dy * up.z,
    }),
  };
};

/**
 * Centre on a box and back off until all of it is in view, in any of the
 * three views. `bounds` is { min, max } in the scene frame; `fovDeg` is the
 * vertical field of view and `aspect` the viewport's width / height.
 */
export const frameBounds = (cam, bounds, { fovDeg = 60, aspect = 1 } = {}) => {
  if (!bounds) return cam;
  const { min, max } = bounds;
  const sphereRadius = Math.hypot(max.x - min.x, max.y - min.y, max.z - min.z) / 2;
  // The narrower of the vertical and horizontal half-angles has to hold the
  // box's bounding sphere.
  const halfV = (fovDeg * Math.PI) / 360;
  const halfFov = Math.min(halfV, Math.atan(Math.tan(halfV) * aspect));
  const fit = (sphereRadius / Math.sin(halfFov)) * FRAME_MARGIN;
  return {
    ...cam,
    target: clampTarget({
      x: (min.x + max.x) / 2, y: (min.y + max.y) / 2, z: (min.z + max.z) / 2,
    }),
    radius: clamp(Math.max(FRAME_MIN_RADIUS, fit), MIN_RADIUS, MAX_RADIUS),
  };
};

const boundsOf = (points) => {
  let bounds = null;
  for (const p of points) {
    if (!bounds) {
      bounds = { min: { ...p }, max: { ...p } };
      continue;
    }
    for (const k of ['x', 'y', 'z']) {
      if (p[k] < bounds.min[k]) bounds.min[k] = p[k];
      if (p[k] > bounds.max[k]) bounds.max[k] = p[k];
    }
  }
  return bounds;
};

/** Box around every waypoint of a built show, or null for an empty one. */
export const showBounds = (show) => {
  const points = [];
  for (const d of show?.drones ?? []) {
    for (const wp of d.waypoints) points.push(toScene(wp));
  }
  return boundsOf(points);
};

/** Box around the fleet at playback time `t`, or null for an empty show. */
export const fleetBoundsAt = (show, t) => {
  const points = [];
  for (const d of show?.drones ?? []) {
    const wp = interpolateWaypoint(d.waypoints, t);
    if (wp) points.push(toScene(wp));
  }
  return boundsOf(points);
};
