import { describe, it, expect } from 'vitest';
import { applyCommand } from './commands.js';
import { defaultFleet, fleetCommands, makeDrone } from './fleetCommands.js';
import { createHistory, execute, undo, redo } from './history.js';
import { PAD_SPACING } from './layout.js';

const doc = (n) => ({ scenes: [], dronesList: defaultFleet(n) });
const run = (d, ...commands) => commands.reduce((acc, c) => applyCommand(acc, c), d);

describe('fleet commands', () => {
  it('add reuses the lowest free id and keeps the list ordered', () => {
    const d = run(doc(4), fleetCommands.remove(1), fleetCommands.add());
    expect(d.dronesList.map((x) => x.id)).toEqual([0, 1, 2, 3]);
  });

  it("a new drone's default spawn Y tracks the live pad grid spacing (issue #132/#167)", () => {
    // Regression: this field used to be hardcoded at the old 2.0m pad
    // spacing and silently went stale when #167 widened the grid to 2.2m.
    const drone = makeDrone(3);
    expect(drone.spawnOffset).toBeCloseTo(PAD_SPACING * 3, 5);
  });

  it('remove never empties the fleet', () => {
    const one = doc(1);
    expect(applyCommand(one, fleetCommands.remove(0))).toBe(one);
  });

  it('update edits one vehicle and sanitises the value', () => {
    const d = run(doc(3), fleetCommands.update(1, 'hoverAlt', -4), fleetCommands.update(2, 'colorHex', '#ABCDEF'));
    expect(d.dronesList[1].hoverAlt).toBe(0);
    expect(d.dronesList[2].colorHex).toBe('#abcdef');
    expect(d.dronesList[0]).toEqual(makeDrone(0));
  });

  it('ignores bad values and unknown fields instead of corrupting the fleet', () => {
    const d = doc(2);
    for (const c of [
      fleetCommands.update(0, 'hoverAlt', Number.NaN),
      fleetCommands.update(0, 'colorHex', 'red'),
      fleetCommands.update(0, 'id', 99),
      fleetCommands.update(42, 'name', 'ghost'),
      fleetCommands.bulkUpdate([], { hoverAlt: 5 }),
      fleetCommands.update(0, 'name', 'Drone 00'),
    ]) expect(applyCommand(d, c)).toBe(d);
  });

  it('set-count grows and shrinks within 1..MAX_FLEET_SIZE', () => {
    expect(run(doc(3), fleetCommands.setCount(100)).dronesList).toHaveLength(100);
    expect(run(doc(3), fleetCommands.setCount(0)).dronesList).toHaveLength(1);
    expect(run(doc(3), fleetCommands.setCount(5000)).dronesList).toHaveLength(1000);
  });

  it('a document without a fleet ignores fleet commands', () => {
    const d = { scenes: [] };
    expect(applyCommand(d, fleetCommands.add())).toBe(d);
  });
});

describe('bulk edit (#102 acceptance: 100 drones, one action)', () => {
  it('sets hover altitude for a selected subset of a 100-drone fleet in one command', () => {
    const start = doc(100);
    const subset = start.dronesList.filter((d) => d.id % 3 === 0).map((d) => d.id);
    const after = applyCommand(start, fleetCommands.bulkUpdate(subset, { hoverAlt: 12.5 }));

    for (const d of after.dronesList) {
      if (subset.includes(d.id)) expect(d.hoverAlt).toBe(12.5);
      else expect(d).toBe(start.dronesList[d.id]); // untouched, same object
    }
  });

  it('is a single undo step however many vehicles it touches', () => {
    const start = doc(100);
    const all = start.dronesList.map((d) => d.id);
    let h = createHistory(start);
    h = execute(h, applyCommand, fleetCommands.bulkUpdate(all, { hoverAlt: 20, colorHex: '#ff0000' }), 0);
    expect(h.past).toHaveLength(1);
    expect(h.past[0].label).toBe('Set hover altitude, LED colour for 100 vehicles');
    expect(h.present.dronesList.every((d) => d.hoverAlt === 20 && d.colorHex === '#ff0000')).toBe(true);

    expect(undo(h).present).toBe(start);
    expect(redo(undo(h)).present).toBe(h.present);
  });

  it('typing a call sign merges into one step; separate vehicles do not', () => {
    let h = createHistory(doc(2));
    ['A', 'Al', 'Alpha'].forEach((v, i) => { h = execute(h, applyCommand, fleetCommands.update(0, 'name', v), i * 100); });
    h = execute(h, applyCommand, fleetCommands.update(1, 'name', 'Bravo'), 400);
    expect(h.past.map((e) => e.label)).toEqual(['Edit call sign', 'Edit call sign']);
    expect(undo(h).present.dronesList[0].name).toBe('Alpha');
  });

  it('fleet and scene edits share one history', () => {
    let h = createHistory({ scenes: [{ id: 's1', name: 'A' }], dronesList: defaultFleet(2) });
    h = execute(h, applyCommand, { type: 'scene/patch', id: 's1', patch: { name: 'B' } }, 0);
    h = execute(h, applyCommand, fleetCommands.setCount(5), 5000);
    h = undo(h);
    expect(h.present.dronesList).toHaveLength(2);
    expect(h.present.scenes[0].name).toBe('B');
  });
});
