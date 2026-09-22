// Web Audio soundtrack engine.
//
// Drone shows are cut to music, so the audio clock — not a rAF accumulator —
// has to be the authority on show time. `performance.now()` and an
// AudioContext's clock drift apart over a few minutes, and a show that drifts
// half a second from its soundtrack is a failed show. So when a track is
// loaded, AudioClock.time() reads the context clock directly and the playback
// hook follows it.
//
// Placed in engine/ alongside lighting.js and sequencer.js rather than the
// utils/ path issue #14 suggests, to match the layout the package already
// uses.

/** File extensions the picker accepts. decodeAudioData handles the rest. */
export const AUDIO_ACCEPT = '.mp3,.wav,.ogg,audio/*';

/**
 * Reads a File into an ArrayBuffer.
 *
 * FileReader rather than File.arrayBuffer() because the issue calls for it and
 * because it reports progress on the large files a full show soundtrack can be.
 */
export const readFileAsArrayBuffer = (file, onProgress) =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error || new Error('Could not read the audio file.'));
    if (onProgress) {
      reader.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded / e.total);
      };
    }
    reader.readAsArrayBuffer(file);
  });

/**
 * Reduces a decoded buffer to per-bucket {min,max} peaks for waveform drawing.
 *
 * Storing both bounds (rather than one RMS value) is what makes the rendered
 * waveform look like a waveform: audio is symmetric about zero, so a single
 * magnitude would draw a half-wave. Buckets are computed once at a resolution
 * high enough for the deepest zoom, then sub-sampled while drawing — decoding
 * is slow, and re-scanning a multi-minute buffer on every zoom change would
 * stall the UI.
 *
 * @param {AudioBuffer} buffer
 * @param {number} bucketCount
 * @returns {{min: Float32Array, max: Float32Array, count: number}}
 */
export const computePeaks = (buffer, bucketCount = 8000) => {
  const count = Math.max(1, Math.floor(bucketCount));
  const min = new Float32Array(count);
  const max = new Float32Array(count);

  const channels = buffer.numberOfChannels;
  const frames = buffer.length;
  const per = frames / count;

  // Mixing to mono as we scan avoids allocating a second full-length buffer;
  // a stereo show track can be tens of megabytes.
  const data = [];
  for (let ch = 0; ch < channels; ch += 1) data.push(buffer.getChannelData(ch));

  for (let b = 0; b < count; b += 1) {
    const start = Math.floor(b * per);
    const end = Math.min(frames, Math.floor((b + 1) * per));
    let lo = 0;
    let hi = 0;
    for (let i = start; i < end; i += 1) {
      let sum = 0;
      for (let ch = 0; ch < channels; ch += 1) sum += data[ch][i];
      const v = sum / channels;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    min[b] = lo;
    max[b] = hi;
  }

  return { min, max, count };
};

/**
 * Draws peaks onto a canvas for a visible time window.
 *
 * Zoom is expressed as the [startTime, endTime] window the caller wants, so
 * the same routine serves 1x/2x/5x without knowing about zoom levels. The
 * canvas is sized to its CSS box times devicePixelRatio so the trace stays
 * crisp on a HiDPI display instead of blurring.
 *
 * @param {HTMLCanvasElement} canvas
 * @param {{min:Float32Array,max:Float32Array,count:number}} peaks
 * @param {object} opts
 * @param {number} opts.duration    full track length (s)
 * @param {number} opts.startTime   left edge of the visible window (s)
 * @param {number} opts.endTime     right edge (s)
 * @param {string} opts.color       waveform fill
 * @param {string} [opts.baseline]  centre-line colour
 * @param {number} [opts.devicePixelRatio] overrides the detected ratio
 */
export const drawWaveform = (canvas, peaks, opts) => {
  if (!canvas || !peaks) return;
  const {
    duration, startTime = 0, endTime = duration,
    color = '#38bdf8', baseline = 'rgba(148,163,184,0.35)',
  } = opts || {};

  // Read the ratio off the canvas's own document rather than a global, so the
  // routine stays callable outside a browser (tests) and correct on a window
  // dragged to a second display with a different scale factor.
  const dpr = opts?.devicePixelRatio
    ?? canvas.ownerDocument?.defaultView?.devicePixelRatio
    ?? (typeof window !== 'undefined' ? window.devicePixelRatio : 1)
    ?? 1;
  const cssW = canvas.clientWidth || 1;
  const cssH = canvas.clientHeight || 1;
  // Only resize when it actually changed — assigning width/height clears the
  // canvas, so doing it unconditionally would flicker on every redraw.
  if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
  }

  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);

  const mid = cssH / 2;

  ctx.strokeStyle = baseline;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(0, mid);
  ctx.lineTo(cssW, mid);
  ctx.stroke();

  const span = Math.max(1e-6, endTime - startTime);
  const total = Math.max(1e-6, duration);

  ctx.fillStyle = color;
  for (let px = 0; px < cssW; px += 1) {
    // Map this pixel back to a bucket range and take the extremes across it,
    // so zooming out averages nothing away — transients stay visible.
    const t0 = startTime + (px / cssW) * span;
    const t1 = startTime + ((px + 1) / cssW) * span;
    const b0 = Math.max(0, Math.min(peaks.count - 1, Math.floor((t0 / total) * peaks.count)));
    const b1 = Math.max(b0, Math.min(peaks.count - 1, Math.floor((t1 / total) * peaks.count)));

    let lo = 0;
    let hi = 0;
    for (let b = b0; b <= b1; b += 1) {
      if (peaks.min[b] < lo) lo = peaks.min[b];
      if (peaks.max[b] > hi) hi = peaks.max[b];
    }

    const yTop = mid - hi * mid;
    const yBot = mid - lo * mid;
    // Always paint at least a hairline, so silence still reads as a track
    // rather than a gap in the canvas.
    ctx.fillRect(px, yTop, 1, Math.max(1, yBot - yTop));
  }
};

/**
 * Playable soundtrack bound to an AudioContext.
 *
 * Wraps the awkward parts of Web Audio playback: a BufferSource is one-shot,
 * so every play/scrub allocates a fresh node, and the show's clock has to be
 * derived from context.currentTime rather than counted in JS.
 */
export class AudioClock {
  constructor() {
    this.ctx = null;
    this.buffer = null;
    this.source = null;
    this.gain = null;
    this.startedAt = 0;    // context time when the current source started
    this.offset = 0;       // track position that source started from
    this.playing = false;
    this.volume = 1;
  }

  /** Lazily creates the context — before a user gesture it would start suspended. */
  ensureContext() {
    if (!this.ctx) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) throw new Error('Web Audio is not available in this browser.');
      this.ctx = new Ctor();
      this.gain = this.ctx.createGain();
      this.gain.gain.value = this.volume;
      this.gain.connect(this.ctx.destination);
    }
    return this.ctx;
  }

  /** Decodes an ArrayBuffer into the playable buffer. Returns its duration. */
  async load(arrayBuffer) {
    const ctx = this.ensureContext();
    // decodeAudioData detaches the ArrayBuffer it's given, so decode a copy —
    // otherwise re-decoding the same file (e.g. after a failed load) throws.
    this.buffer = await ctx.decodeAudioData(arrayBuffer.slice(0));
    this.stop();
    this.offset = 0;
    return this.buffer.duration;
  }

  get duration() {
    return this.buffer ? this.buffer.duration : 0;
  }

  /** Current playhead position in the track, in seconds. */
  time() {
    if (!this.buffer) return 0;
    if (!this.playing) return this.offset;
    const t = this.offset + (this.ctx.currentTime - this.startedAt);
    return Math.max(0, Math.min(this.buffer.duration, t));
  }

  async play(fromSeconds) {
    if (!this.buffer) return;
    const ctx = this.ensureContext();
    // Autoplay policy suspends the context until a gesture resumes it.
    if (ctx.state === 'suspended') await ctx.resume();

    this.stop();
    const at = fromSeconds === undefined ? this.offset : fromSeconds;
    const src = ctx.createBufferSource();
    src.buffer = this.buffer;
    src.connect(this.gain);
    src.start(0, Math.max(0, Math.min(this.buffer.duration, at)));

    this.source = src;
    this.offset = at;
    this.startedAt = ctx.currentTime;
    this.playing = true;
  }

  /** Stops the current source without moving the playhead. */
  stop() {
    if (this.source) {
      // The source fires onended as it stops; detach first so a stale handler
      // can't clear state belonging to the source that replaces it.
      this.source.onended = null;
      try { this.source.stop(); } catch { /* already stopped */ }
      this.source.disconnect();
      this.source = null;
    }
    this.playing = false;
  }

  /** Pauses, keeping the playhead where it is. */
  pause() {
    const at = this.time();
    this.stop();
    this.offset = at;
  }

  /** Moves the playhead, continuing playback if it was running. */
  seek(seconds) {
    const wasPlaying = this.playing;
    this.stop();
    this.offset = Math.max(0, Math.min(this.duration, seconds));
    if (wasPlaying) this.play(this.offset);
  }

  setVolume(v) {
    this.volume = Math.max(0, Math.min(1, v));
    if (this.gain) this.gain.gain.value = this.volume;
  }

  /** Releases the context. Browsers cap how many can exist at once. */
  dispose() {
    this.stop();
    this.buffer = null;
    if (this.ctx) {
      this.ctx.close();
      this.ctx = null;
      this.gain = null;
    }
  }
}
