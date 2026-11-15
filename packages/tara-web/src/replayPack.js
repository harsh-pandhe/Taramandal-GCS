// Packs the frames of a replay window into typed arrays and back (issue #284).
//
// A full-rate window at 1,000 drones is about 2.7 MB of JSON. Parsing that on the main thread cost a frame
// each time the replay swapped windows, so a worker (replayWorker.js) fetches and parses it and hands the
// numbers back as typed arrays, which are transferred rather than copied. The channels the replay draws from
// are kept (time, ids, x/y/z, heading, flags); the rest of a frame is dropped. unpackFrames rebuilds the
// frame objects fillFleetBuffer and telemetryAt read, as views into the transferred buffers (no copy), so
// `frame.x_m[k]` works the same on them as on the JSON arrays.

/** @param {object[]} frames API frames: { t_ms, ids, x_m, y_m, z_m, heading_deg?, flags? } */
export function packFrames(frames) {
  const n = frames.length;
  let total = 0;
  for (const f of frames) total += f.ids.length;
  const packed = {
    t: new Float64Array(n),
    counts: new Int32Array(n),
    ids: new Int32Array(total),
    x: new Float32Array(total),
    y: new Float32Array(total),
    z: new Float32Array(total),
    h: new Float32Array(total),
    flags: new Int32Array(total),
  };
  let o = 0;
  frames.forEach((f, i) => {
    packed.t[i] = f.t_ms;
    packed.counts[i] = f.ids.length;
    for (let k = 0; k < f.ids.length; k += 1) {
      packed.ids[o] = f.ids[k];
      packed.x[o] = f.x_m[k];
      packed.y[o] = f.y_m[k];
      packed.z[o] = f.z_m[k];
      packed.h[o] = f.heading_deg ? (f.heading_deg[k] ?? 0) : 0;
      packed.flags[o] = f.flags ? (f.flags[k] ?? 0) : 0;
      o += 1;
    }
  });
  return packed;
}

/** The buffers of a packed window, for a postMessage transfer list. */
export const packedBuffers = (p) => [p.t.buffer, p.counts.buffer, p.ids.buffer, p.x.buffer, p.y.buffer, p.z.buffer, p.h.buffer, p.flags.buffer];

/** Frame objects over a packed window: typed-array views, no copying. */
export function unpackFrames(p) {
  const frames = new Array(p.t.length);
  let o = 0;
  for (let i = 0; i < frames.length; i += 1) {
    const c = p.counts[i];
    frames[i] = {
      t_ms: p.t[i],
      ids: p.ids.subarray(o, o + c),
      x_m: p.x.subarray(o, o + c),
      y_m: p.y.subarray(o, o + c),
      z_m: p.z.subarray(o, o + c),
      heading_deg: p.h.subarray(o, o + c),
      flags: p.flags.subarray(o, o + c),
    };
    o += c;
  }
  return frames;
}
