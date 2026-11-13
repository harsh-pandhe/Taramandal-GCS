// Decodes the fast-channel binary telemetry frame. Mirrors
// packages/tara-gcs/tara_gcs/telemetry_codec.py's FAST_FIELDS layout exactly —
// keep the two in sync by hand; there is no shared schema between the
// Python and JS sides.
export const FAST_FIELDS = [
  'fleet_x', 'fleet_y', 'fleet_z',
  'local_x', 'local_y', 'local_z',
  'heading', 'armed', 'connected',
  'planned_x', 'planned_y', 'planned_z', 'planned_valid',
  // The LED colour (0-255) the show commands right now, from the colours Studio
  // bakes into the waypoints; led_valid is 0 when the show carries none.
  'led_r', 'led_g', 'led_b', 'led_valid',
];
export const FLOATS_PER_DRONE = FAST_FIELDS.length;
export const BYTES_PER_DRONE = FLOATS_PER_DRONE * 4;

// Every fast frame starts with [FRAME_MAGIC, FRAME_VERSION, floats per drone]
// (telemetry_codec.py documents why). A frame whose header is not exactly what
// this build expects is refused, never decoded: reading another layout's
// records at this layout's stride yields plausible-looking wrong positions.
export const FRAME_MAGIC = 7243;
export const FRAME_VERSION = 1;
export const HEADER_FLOATS = 3;

export class FrameFormatError extends Error {
  constructor(message) {
    super(message);
    this.name = 'FrameFormatError';
  }
}

// Reads a fast frame: returns { floats, n } (the whole frame as a Float32Array
// and its drone count), or throws FrameFormatError. A byte length that is not a
// multiple of 4 is checked first, because `new Float32Array` would throw a plain
// RangeError that the caller does not expect.
function readFrame(buffer) {
  if (buffer.byteLength % 4 !== 0) {
    throw new FrameFormatError(
      `telemetry frame of ${buffer.byteLength} bytes is not a whole number of 32-bit words`,
    );
  }
  const floats = new Float32Array(buffer);
  if (floats.length < HEADER_FLOATS) {
    throw new FrameFormatError(
      `telemetry frame of ${buffer.byteLength} bytes is too short to carry a header`,
    );
  }
  if (floats[0] !== FRAME_MAGIC) {
    throw new FrameFormatError(
      'telemetry frame has no recognised header (the GCS is older than this dashboard, or the stream is not telemetry)',
    );
  }
  if (floats[1] !== FRAME_VERSION || floats[2] !== FLOATS_PER_DRONE) {
    throw new FrameFormatError(
      `telemetry frame is layout v${floats[1]} with ${floats[2]} fields per drone; ` +
        `this dashboard reads v${FRAME_VERSION} with ${FLOATS_PER_DRONE}`,
    );
  }
  const body = floats.length - HEADER_FLOATS;
  if (body % FLOATS_PER_DRONE !== 0) {
    throw new FrameFormatError(
      `telemetry frame body of ${body} floats is not a whole number of ${FLOATS_PER_DRONE}-field records`,
    );
  }
  return { floats, n: body / FLOATS_PER_DRONE };
}

// Returns an array of per-drone objects, index === drone id (server keys are
// always a contiguous range(len(ports)), so sorted position and id coincide).
export function decodeFastFrame(buffer) {
  const { floats, n } = readFrame(buffer);
  const drones = new Array(n);
  for (let i = 0; i < n; i++) {
    const base = HEADER_FLOATS + i * FLOATS_PER_DRONE;
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
      planned_x: floats[base + 9],
      planned_y: floats[base + 10],
      planned_z: floats[base + 11],
      planned_valid: floats[base + 12] !== 0,
      led_r: floats[base + 13],
      led_g: floats[base + 14],
      led_b: floats[base + 15],
      led_valid: floats[base + 16] !== 0,
    };
  }
  return drones;
}

// Decodes straight into a caller-owned Float32Array view rather than allocating
// per-drone objects. The night-sky viewport pushes telemetry into instance
// buffers every frame at up to 1000 drones — going through 1000 fresh objects
// per frame at 20Hz is what turns this path into GC pressure, which shows up as
// exactly the periodic frame-time spikes the >=55 FPS budget can't absorb.
// Returns the drone count actually decoded; throws FrameFormatError like
// decodeFastFrame.
export function decodeFastFrameInto(buffer, out) {
  const { floats, n: frameN } = readFrame(buffer);
  const n = Math.min(frameN, Math.floor(out.length / FLOATS_PER_DRONE));
  out.set(floats.subarray(HEADER_FLOATS, HEADER_FLOATS + n * FLOATS_PER_DRONE));
  return n;
}
