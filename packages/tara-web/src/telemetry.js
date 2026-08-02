// Decodes the fast-channel binary telemetry frame. Mirrors
// packages/tara-gcs/tara_gcs/telemetry_codec.py's FAST_FIELDS layout exactly —
// keep the two in sync by hand; there is no shared schema between the
// Python and JS sides.
export const FAST_FIELDS = [
  'fleet_x', 'fleet_y', 'fleet_z',
  'local_x', 'local_y', 'local_z',
  'heading', 'armed', 'connected',
];
export const FLOATS_PER_DRONE = FAST_FIELDS.length;
export const BYTES_PER_DRONE = FLOATS_PER_DRONE * 4;

// Returns an array of per-drone objects, index === drone id (server keys are
// always a contiguous range(len(ports)), so sorted position and id coincide).
export function decodeFastFrame(buffer) {
  const floats = new Float32Array(buffer);
  const n = Math.floor(floats.length / FLOATS_PER_DRONE);
  const drones = new Array(n);
  for (let i = 0; i < n; i++) {
    const base = i * FLOATS_PER_DRONE;
    drones[i] = {
      fleet_x: floats[base],
      fleet_y: floats[base + 1],
      fleet_z: floats[base + 2],
      local_x: floats[base + 3],
      local_y: floats[base + 4],
      local_z: floats[base + 5],
      heading: floats[base + 6],
      armed: floats[base + 7] !== 0,
      connected: floats[base + 8] !== 0,
    };
  }
  return drones;
}
