// Search, selection and windowing for the fleet list (issue #102). Pure, so
// the page component stays thin and these rules are unit-tested directly.

/**
 * Vehicles matching `query`, case-insensitively:
 *   "#38bd"   LED colour (needs the "#", so a number never matches colours)
 *   "7"/"07"  vehicle number, plus any call sign containing it
 *   "alpha"   call-sign substring
 * An empty query matches everything.
 */
export const filterDrones = (drones, query) => {
  const q = (query || '').trim().toLowerCase();
  if (!q) return drones;
  if (q.startsWith('#')) return drones.filter((d) => d.colorHex.toLowerCase().startsWith(q));
  const asNumber = /^\d+$/.test(q) ? Number(q) : null;
  return drones.filter((d, idx) => d.name.toLowerCase().includes(q) || idx === asNumber);
};

/** Toggles one id in a selection Set, returning a new Set. */
export const toggle = (selected, id) => {
  const next = new Set(selected);
  if (next.has(id)) next.delete(id); else next.add(id);
  return next;
};

/**
 * Shift-click: every id between the anchor and `id` in the order shown gets
 * the state `id` is being set to. Falls back to a plain toggle when the
 * anchor is not in the visible list.
 */
export const selectRange = (selected, orderedIds, anchorId, id) => {
  const a = orderedIds.indexOf(anchorId);
  const b = orderedIds.indexOf(id);
  if (a < 0 || b < 0) return toggle(selected, id);
  const on = !selected.has(id);
  const next = new Set(selected);
  for (const rangeId of orderedIds.slice(Math.min(a, b), Math.max(a, b) + 1)) {
    if (on) next.add(rangeId); else next.delete(rangeId);
  }
  return next;
};

/** Select-all over what is shown: selects every shown id, or clears them if all are already selected. */
export const toggleAll = (selected, shownIds) => {
  const allOn = shownIds.length > 0 && shownIds.every((id) => selected.has(id));
  const next = new Set(selected);
  for (const id of shownIds) {
    if (allOn) next.delete(id); else next.add(id);
  }
  return next;
};

/** Drops ids that no longer exist (a vehicle was removed or the fleet shrank). */
export const prune = (selected, drones) => {
  const live = new Set(drones.map((d) => d.id));
  const next = new Set([...selected].filter((id) => live.has(id)));
  return next.size === selected.size ? selected : next;
};

/**
 * Rows to render for a list of `count` fixed-height rows scrolled to
 * `scrollTop` in a viewport `viewportPx` tall, with `overscan` extra rows
 * either side so fast scrolling never shows a blank gap.
 */
export const visibleRange = (scrollTop, viewportPx, rowPx, count, overscan = 4) => {
  if (count <= 0) return { start: 0, end: 0 };
  const first = Math.floor(Math.max(0, scrollTop) / rowPx);
  const last = Math.ceil((Math.max(0, scrollTop) + viewportPx) / rowPx);
  return { start: Math.max(0, first - overscan), end: Math.min(count, last + overscan) };
};
