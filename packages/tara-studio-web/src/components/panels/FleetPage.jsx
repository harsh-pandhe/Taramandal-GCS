import { useMemo, useRef, useState, useEffect, useCallback } from 'react';
import { Plus, X, Search } from 'lucide-react';
import { NumberField } from '../common/index.jsx';
import HistoryBar from '../sequencer/HistoryBar.jsx';
import { MAX_FLEET_SIZE } from '../../engine/api.js';
import {
  filterDrones, prune, selectRange, toggle, toggleAll, visibleRange,
} from '../../engine/fleetSelection.js';

/** Height of one vehicle row; the windowing maths relies on it being fixed. */
export const ROW_PX = 44;
/** Viewport height assumed until the list is measured (and in static renders). */
const DEFAULT_VIEWPORT_PX = 560;

const label = (idx) => `VEHICLE ${String(idx).padStart(2, '0')}`;

const parseNumber = (raw) => {
  const v = parseFloat(raw);
  return Number.isFinite(v) ? v : null;
};

/**
 * Fleet editing for 50-100+ vehicles (issue #102).
 *
 * One row per vehicle in a windowed list: only the rows in view (plus a few
 * either side) are in the DOM, so a 100-drone fleet has ~20 colour pickers
 * mounted instead of 100. Vehicles can be searched, multi-selected (checkbox,
 * shift-click for a range, select-all over what is shown) and edited in bulk;
 * every edit, bulk or single, is an undoable command (#100).
 */
export default function FleetPage({
  dronesList, onAdd, onRemove, onUpdate, onBulkUpdate, onSetCount, history,
}) {
  const [query, setQuery] = useState('');
  const [rawSelected, setSelected] = useState(() => new Set());
  const [anchor, setAnchor] = useState(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportPx, setViewportPx] = useState(DEFAULT_VIEWPORT_PX);
  const [bulk, setBulk] = useState({ hoverAlt: '', spawnOffset: '', colorHex: '#38bdf8' });
  const listRef = useRef(null);

  const indexOf = useMemo(() => new Map(dronesList.map((d, i) => [d.id, i])), [dronesList]);
  const shown = useMemo(() => filterDrones(dronesList, query), [dronesList, query]);
  const shownIds = useMemo(() => shown.map((d) => d.id), [shown]);
  // Removed vehicles drop out of the selection.
  const selected = useMemo(() => prune(rawSelected, dronesList), [rawSelected, dronesList]);
  const hiddenSelected = useMemo(
    () => [...selected].filter((id) => !shownIds.includes(id)).length,
    [selected, shownIds],
  );
  const allShownSelected = shownIds.length > 0 && shownIds.every((id) => selected.has(id));

  useEffect(() => {
    const el = listRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const measure = () => setViewportPx(el.clientHeight || DEFAULT_VIEWPORT_PX);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // A new search starts at the top of its results.
  useEffect(() => {
    setScrollTop(0);
    if (listRef.current) listRef.current.scrollTop = 0;
  }, [query]);

  const { start, end } = visibleRange(scrollTop, viewportPx, ROW_PX, shown.length);

  const onSelect = useCallback((id, shiftKey) => {
    setSelected((prev) => (shiftKey && anchor !== null
      ? selectRange(prune(prev, dronesList), shownIds, anchor, id)
      : toggle(prune(prev, dronesList), id)));
    setAnchor(id);
  }, [anchor, shownIds, dronesList]);

  const applyBulk = (field) => {
    const value = field === 'colorHex' ? bulk.colorHex : parseNumber(bulk[field]);
    if (value === null || !selected.size) return;
    onBulkUpdate([...selected], { [field]: value });
  };

  const nothingSelected = selected.size === 0;

  return (
    <main className="studio-viewport-page fleet-page">
      <div className="page-header">
        <div>
          <h2>Swarm Fleet</h2>
          <p>Vehicle call signs, spawn offsets, hover altitude and LED colour.</p>
        </div>
        <div className="page-header-actions">
          <NumberField
            label="Fleet size" min={1} max={MAX_FLEET_SIZE}
            value={dronesList.length} onChange={(v) => onSetCount(Math.round(v))}
          />
          <button type="button" className="btn-primary btn-inline" onClick={onAdd} disabled={dronesList.length >= MAX_FLEET_SIZE}>
            <Plus size={15} /> Add Vehicle
          </button>
        </div>
      </div>

      <div className="fleet-toolbar">
        <label className="fleet-search">
          <Search size={13} />
          <input
            type="search" value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="Search call sign, number or #colour" aria-label="Search vehicles"
          />
        </label>
        <span className="fleet-count" data-testid="fleet-count">
          {shown.length === dronesList.length ? `${dronesList.length} vehicles` : `${shown.length} of ${dronesList.length} shown`}
          {' · '}
          <strong>{selected.size} selected</strong>
          {hiddenSelected > 0 && ` (${hiddenSelected} not shown)`}
        </span>
        {history && <HistoryBar history={history} />}
      </div>

      <div className="fleet-bulk" role="group" aria-label="Edit selected vehicles">
        <span className="fleet-bulk-title">Set for selected:</span>
        <label>
          Hover alt (m)
          <input
            type="number" step={0.1} min={0} value={bulk.hoverAlt} aria-label="Bulk hover altitude"
            onChange={(e) => setBulk((b) => ({ ...b, hoverAlt: e.target.value }))}
          />
        </label>
        <button type="button" className="btn-secondary btn-sm" disabled={nothingSelected || parseNumber(bulk.hoverAlt) === null} onClick={() => applyBulk('hoverAlt')}>
          Apply
        </button>
        <label>
          Spawn Y (m)
          <input
            type="number" step={0.5} value={bulk.spawnOffset} aria-label="Bulk spawn Y"
            onChange={(e) => setBulk((b) => ({ ...b, spawnOffset: e.target.value }))}
          />
        </label>
        <button type="button" className="btn-secondary btn-sm" disabled={nothingSelected || parseNumber(bulk.spawnOffset) === null} onClick={() => applyBulk('spawnOffset')}>
          Apply
        </button>
        <label>
          LED colour
          <input
            type="color" value={bulk.colorHex} aria-label="Bulk LED colour"
            onChange={(e) => setBulk((b) => ({ ...b, colorHex: e.target.value }))}
          />
        </label>
        <button type="button" className="btn-secondary btn-sm" disabled={nothingSelected} onClick={() => applyBulk('colorHex')}>
          Apply
        </button>
        <button type="button" className="btn-secondary btn-sm" disabled={nothingSelected} onClick={() => setSelected(new Set())}>
          Clear selection
        </button>
      </div>

      <div className="fleet-table" role="table" aria-label="Vehicles" aria-rowcount={shown.length}>
        <div className="fleet-row fleet-row-head" role="row">
          <span role="columnheader">
            <input
              type="checkbox" aria-label="Select all shown"
              checked={allShownSelected} onChange={() => setSelected((prev) => toggleAll(prune(prev, dronesList), shownIds))}
            />
          </span>
          <span role="columnheader">Vehicle</span>
          <span role="columnheader">Call sign</span>
          <span role="columnheader">Spawn Y (m)</span>
          <span role="columnheader">Hover alt (m)</span>
          <span role="columnheader">LED colour</span>
          <span role="columnheader" />
        </div>

        <div
          className="fleet-rows" ref={listRef} data-testid="fleet-rows"
          onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        >
          {shown.length === 0 && <div className="fleet-empty">No vehicle matches “{query}”.</div>}
          <div style={{ height: shown.length * ROW_PX, position: 'relative' }}>
            {shown.slice(start, end).map((drone, i) => {
              const idx = indexOf.get(drone.id);
              const isSelected = selected.has(drone.id);
              return (
                <div
                  key={drone.id} role="row" aria-rowindex={start + i + 1}
                  className={`fleet-row${isSelected ? ' selected' : ''}`}
                  style={{ position: 'absolute', top: (start + i) * ROW_PX, height: ROW_PX, borderLeftColor: drone.colorHex }}
                >
                  <span role="cell">
                    <input
                      type="checkbox" aria-label={`Select ${label(idx)}`} checked={isSelected}
                      onChange={() => {}} onClick={(e) => onSelect(drone.id, e.shiftKey)}
                    />
                  </span>
                  <span role="cell" className="drone-id-badge" style={{ color: drone.colorHex }}>{label(idx)}</span>
                  <span role="cell">
                    <input
                      value={drone.name} aria-label={`Call sign of ${label(idx)}`}
                      onChange={(e) => onUpdate(drone.id, 'name', e.target.value)}
                    />
                  </span>
                  <span role="cell">
                    <input
                      type="number" step={0.5} value={drone.spawnOffset} aria-label={`Spawn Y of ${label(idx)}`}
                      onChange={(e) => { const v = parseNumber(e.target.value); if (v !== null) onUpdate(drone.id, 'spawnOffset', v); }}
                    />
                  </span>
                  <span role="cell">
                    <input
                      type="number" step={0.1} min={0} value={drone.hoverAlt} aria-label={`Hover altitude of ${label(idx)}`}
                      onChange={(e) => { const v = parseNumber(e.target.value); if (v !== null) onUpdate(drone.id, 'hoverAlt', v); }}
                    />
                  </span>
                  <span role="cell" className="color-row">
                    <input
                      type="color" value={drone.colorHex} aria-label={`LED colour of ${label(idx)}`}
                      onChange={(e) => onUpdate(drone.id, 'colorHex', e.target.value)}
                    />
                    <span className="mono">{drone.colorHex.toUpperCase()}</span>
                  </span>
                  <span role="cell">
                    <button
                      type="button" className="delete-drone-btn"
                      onClick={() => onRemove(drone.id)} disabled={dronesList.length <= 1}
                      aria-label={`Remove ${label(idx)}`}
                    >
                      <X size={15} />
                    </button>
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </main>
  );
}
