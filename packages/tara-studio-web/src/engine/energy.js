// Pure helpers for the endurance (battery) section of the Safety tab — kept
// out of the components so they can be unit-tested without a DOM.

/** Pack settings sent to /api/validate-energy. Mirrors tara_studio.energy's defaults. */
export const DEFAULT_BATTERY = {
  chemistry: 'lipo',
  cells_series: 4,
  capacity_mah: 5000,
  ambient_temp_c: 25,
};

const STORAGE_KEY = 'tara-studio.battery';

/**
 * The designer's last-used pack, so it survives reloads. Storage can be
 * missing or throw (private windows, blocked site data); the defaults always
 * stand in.
 */
export function loadBatterySettings(storage = globalThis.localStorage) {
  try {
    const raw = storage?.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_BATTERY };
    const saved = JSON.parse(raw);
    const merged = { ...DEFAULT_BATTERY };
    for (const key of Object.keys(DEFAULT_BATTERY)) {
      if (typeof saved?.[key] === typeof DEFAULT_BATTERY[key]) merged[key] = saved[key];
    }
    return merged;
  } catch {
    return { ...DEFAULT_BATTERY };
  }
}

export function saveBatterySettings(settings, storage = globalThis.localStorage) {
  try {
    storage?.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch {
    // Not persisting is fine; the settings still apply for this session.
  }
}

/** Linear scale from [d0, d1] onto [r0, r1]. A zero-width domain maps to r0. */
export function scaleLinear([d0, d1], [r0, r1]) {
  const span = d1 - d0;
  return (v) => (span === 0 ? r0 : r0 + ((v - d0) / span) * (r1 - r0));
}

/** SVG path `d` for a polyline through (xs[i], ys[i]). */
export function linePath(xs, ys, x, y) {
  return xs.map((t, i) => `${i ? 'L' : 'M'}${x(t).toFixed(1)},${y(ys[i]).toFixed(1)}`).join('');
}

/** Index of the sample in ascending `xs` closest to `value` (the crosshair snap). */
export function nearestIndex(xs, value) {
  if (!xs.length) return -1;
  let lo = 0;
  let hi = xs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] <= value) lo = mid; else hi = mid;
  }
  return Math.abs(xs[hi] - value) < Math.abs(xs[lo] - value) ? hi : lo;
}

/** Every `step`-th index plus the last, for the chart's table view. */
export function tableRows(length, maxRows = 12) {
  if (length <= 0) return [];
  const step = Math.max(1, Math.ceil(length / maxRows));
  const rows = [];
  for (let i = 0; i < length; i += step) rows.push(i);
  if (rows[rows.length - 1] !== length - 1) rows.push(length - 1);
  return rows;
}

/** m:ss for a show-time axis. */
export function formatClock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}
