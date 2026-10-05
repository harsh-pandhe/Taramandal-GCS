import { describe, it, expect } from 'vitest';
import { defaultFleet } from './fleetCommands.js';
import { filterDrones, prune, selectRange, toggle, toggleAll, visibleRange } from './fleetSelection.js';

const fleet = defaultFleet(30).map((d) => (d.id === 4 ? { ...d, name: 'Alpha Leader' } : d));
const ids = (list) => list.map((d) => d.id);

describe('filterDrones', () => {
  it('matches call signs case-insensitively', () => {
    expect(ids(filterDrones(fleet, 'alpha'))).toEqual([4]);
    // "Drone 01" is vehicle 1's name, so "drone 1" finds 10-19.
    expect(ids(filterDrones(fleet, 'DRONE 1'))).toEqual([10, 11, 12, 13, 14, 15, 16, 17, 18, 19]);
  });

  it('matches a vehicle number', () => {
    expect(ids(filterDrones(fleet, '7'))).toContain(7);
    expect(ids(filterDrones(fleet, '07'))).toContain(7);
  });

  it('matches an LED colour only with a leading #', () => {
    const blue = filterDrones(fleet, '#38BD');
    expect(blue.length).toBeGreaterThan(0);
    expect(blue.every((d) => d.colorHex === '#38bdf8')).toBe(true);
    expect(filterDrones(fleet, '38bd')).toEqual([]);
  });

  it('an empty query returns the fleet itself', () => {
    expect(filterDrones(fleet, '  ')).toBe(fleet);
  });
});

describe('selection', () => {
  const order = [1, 2, 3, 4, 5, 6];

  it('toggle adds and removes without mutating', () => {
    const s = new Set([1]);
    expect([...toggle(s, 2)]).toEqual([1, 2]);
    expect([...toggle(s, 1)]).toEqual([]);
    expect([...s]).toEqual([1]);
  });

  it('shift-click selects the range in display order, in either direction', () => {
    expect([...selectRange(new Set([2]), order, 2, 5)].sort()).toEqual([2, 3, 4, 5]);
    expect([...selectRange(new Set([5]), order, 5, 2)].sort()).toEqual([2, 3, 4, 5]);
  });

  it('shift-click onto a selected row clears the range', () => {
    expect([...selectRange(new Set([1, 2, 3, 4]), order, 1, 3)]).toEqual([4]);
  });

  it('shift-click with an anchor no longer shown is a plain toggle', () => {
    expect([...selectRange(new Set(), [5, 6], 1, 6)]).toEqual([6]);
  });

  it('select-all works on what is shown and keeps hidden selections', () => {
    const s = toggleAll(new Set([99]), [1, 2, 3]);
    expect([...s].sort((a, b) => a - b)).toEqual([1, 2, 3, 99]);
    expect([...toggleAll(s, [1, 2, 3])]).toEqual([99]);
    expect([...toggleAll(new Set(), [])]).toEqual([]);
  });

  it('prune drops removed vehicles and keeps identity when nothing changed', () => {
    const s = new Set([0, 1, 50]);
    expect([...prune(s, fleet)]).toEqual([0, 1]);
    const live = new Set([0, 1]);
    expect(prune(live, fleet)).toBe(live);
  });
});

describe('visibleRange', () => {
  it('renders the rows in view plus overscan', () => {
    expect(visibleRange(0, 560, 44, 100, 4)).toEqual({ start: 0, end: 17 });
    expect(visibleRange(44 * 50, 560, 44, 100, 4)).toEqual({ start: 46, end: 67 });
  });

  it('clamps at both ends and handles an empty list', () => {
    expect(visibleRange(44 * 99, 560, 44, 100, 4).end).toBe(100);
    expect(visibleRange(-50, 560, 44, 100, 4).start).toBe(0);
    expect(visibleRange(0, 560, 44, 0)).toEqual({ start: 0, end: 0 });
  });

  it('keeps the rendered row count bounded however large the fleet', () => {
    for (const n of [100, 500, 1000]) {
      const { start, end } = visibleRange(44 * (n / 2), 560, 44, n, 4);
      expect(end - start).toBeLessThanOrEqual(22);
    }
  });
});
