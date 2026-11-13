// Client side of the black-box replay API (issue #65, tara_gcs/blackbox_replay.py).
//
// Everything here is plain functions so the playback maths can be read and
// checked without a browser: where a time falls in the recorded frames, how a
// pair of frames becomes one fleet-buffer record set for the night-sky view, and
// where an event sits on the timeline.

import { API_BASE } from './config';
import { gcsFetch } from './session';
import { FAST_FIELDS, FLOATS_PER_DRONE } from './telemetry';

const OFF = Object.fromEntries(FAST_FIELDS.map((name, i) => [name, i]));

// Bit positions of the per-drone `flags` word (blackbox.FLAG_BITS).
export const FLAG_CONNECTED = 1 << 0;
export const FLAG_ARMED = 1 << 1;

async function getJson(path) {
  const res = await gcsFetch(`${API_BASE}${path}`);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body?.detail;
    throw new Error((detail && typeof detail === 'object' ? detail.message : detail) || `GCS answered ${res.status}`);
  }
  return body;
}

export async function listSessions() {
  return (await getJson('/api/blackbox/sessions')).sessions;
}

/** Summary, the safety events (the timeline markers) and an overview of the whole session. */
export async function loadSession(id) {
  const enc = encodeURIComponent(id);
  const [summary, events, frames] = await Promise.all([
    getJson(`/api/blackbox/sessions/${enc}`),
    getJson(`/api/blackbox/sessions/${enc}/events?safety_only=true`),
    getJson(`/api/blackbox/sessions/${enc}/frames`),
  ]);
  return { summary, events, frames };
}

/** Index of the last frame at or before `tMs` (0 if `tMs` is before the first one). */
export function frameIndexAt(frames, tMs) {
  let lo = 0;
  let hi = frames.length - 1;
  if (hi < 0) return -1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (frames[mid].t_ms <= tMs) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

function sameIds(a, b) {
  if (a === b) return true;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/**
 * Writes the fleet at time `tMs` into `buffer` ({ data: Float32Array, count }) in
 * the layout the night-sky view reads (telemetry.js FAST_FIELDS), positions
 * blended linearly between the two recorded frames either side. Fields a recording
 * does not carry (planned position, LED colour) are written as "not valid", so the
 * view draws the fleet as unmonitored rather than inventing a deviation.
 * Returns the drone count.
 */
export function fillFleetBuffer(frames, tMs, buffer) {
  const i = frameIndexAt(frames, tMs);
  if (i < 0) {
    buffer.count = 0;
    return 0;
  }
  const a = frames[i];
  const b = frames[Math.min(i + 1, frames.length - 1)];
  const span = b.t_ms - a.t_ms;
  const alpha = span > 0 && sameIds(a.ids, b.ids) ? Math.min(1, Math.max(0, (tMs - a.t_ms) / span)) : 0;
  const n = Math.min(a.ids.length, Math.floor(buffer.data.length / FLOATS_PER_DRONE));
  const out = buffer.data;
  for (let k = 0; k < n; k++) {
    const base = k * FLOATS_PER_DRONE;
    const mix = (key) => (alpha === 0 ? a[key][k] : a[key][k] + (b[key][k] - a[key][k]) * alpha);
    out.fill(0, base, base + FLOATS_PER_DRONE);
    out[base + OFF.fleet_x] = mix('x_m');
    out[base + OFF.fleet_y] = mix('y_m');
    out[base + OFF.fleet_z] = mix('z_m');
    out[base + OFF.heading] = a.heading_deg ? a.heading_deg[k] : 0;
    const flags = a.flags ? a.flags[k] : 0;
    out[base + OFF.armed] = flags & FLAG_ARMED ? 1 : 0;
    out[base + OFF.connected] = flags & FLAG_CONNECTED ? 1 : 0;
  }
  buffer.count = n;
  return n;
}

/** Where `tMs` falls on a timeline from `startMs` to `endMs`, as a 0..1 fraction. */
export function timelineFraction(tMs, startMs, endMs) {
  if (!(endMs > startMs)) return 0;
  return Math.min(1, Math.max(0, (tMs - startMs) / (endMs - startMs)));
}

const BREACH_KINDS = new Set(['proximity_breach', 'geofence_hard_breach', 'geofence_soft_breach']);
export const isBreach = (event) => BREACH_KINDS.has(event.kind);

/** "proximity breach" for `proximity_breach`. */
export const eventLabel = (event) => String(event.kind || 'event').replace(/_/g, ' ');

const HIDDEN_EVENT_KEYS = new Set(['type', 'wall', 'kind', 't']);
/** The event's own fields as [name, text] pairs, for the detail panel. */
export function eventFields(event) {
  return Object.entries(event)
    .filter(([key]) => !HIDDEN_EVENT_KEYS.has(key))
    .map(([key, value]) => [key, typeof value === 'object' ? JSON.stringify(value) : String(value)]);
}

export function formatClock(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  return `${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}
