import { describe, it, expect } from 'vitest';
import { applyBlock, describeChanges, violationLines } from './proposalView.js';
import { parseIntent } from './intentParser.js';
import { makeScene } from './sequencer.js';

const mkDoc = () => ({
  scenes: [
    makeScene({ id: 'a', name: 'One' }),
    makeScene({ id: 'b', name: 'Two' }),
    makeScene({ id: 'c', name: 'Three' }),
  ],
  dronesList: [],
});
const cmd = (doc, text) => {
  const r = parseIntent(text, doc);
  if (!r.ok) throw new Error(r.reason);
  return r.command;
};
const rowsOf = (doc, text) => describeChanges(doc, cmd(doc, text)).map((r) => `${r.label}: ${r.value}`);

describe('describeChanges', () => {
  it('lists a timing edit as before -> after', () => {
    expect(rowsOf(mkDoc(), 'set hold of scene 2 to 9 s')).toEqual(['Scene 2 "Two" hold: 6 s → 9 s']);
    expect(rowsOf(mkDoc(), 'set transition of scene 1 to 2.5 s')).toEqual(['Scene 1 "One" transition: 4 s → 2.5 s']);
  });

  it('lists a shape edit with readable names', () => {
    const rows = rowsOf(mkDoc(), 'make scene 1 a heart');
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatch(/^Scene 1 "One" shape: .* → Heart$/);
  });

  it('lists a delete, a duplicate and an add', () => {
    expect(rowsOf(mkDoc(), 'delete scene 3')).toEqual(['Scene 3 "Three": deleted']);
    expect(rowsOf(mkDoc(), 'duplicate scene 1')[0]).toMatch(/^New scene 2 "One copy"/);
    expect(rowsOf(mkDoc(), 'add scene heart')[0]).toMatch(/^New scene 4 /);
  });

  it('lists a move as position changes', () => {
    const rows = rowsOf(mkDoc(), 'move scene 3 before scene 1');
    expect(rows).toContain('Scene "Three" position: 3 → 1');
  });

  it('is empty for a command the document refuses', () => {
    const doc = mkDoc();
    const c = cmd(doc, 'set hold of scene 1 to 8 s');
    expect(describeChanges({ ...doc, scenes: doc.scenes.slice(1) }, c)).toEqual([]);
  });
});

describe('violationLines', () => {
  it('reads proximity and velocity entries and counts the rest', () => {
    const lines = violationLines({
      proximity: [{ time: 6.1, drones: [2, 3], distance: 1.2 }],
      velocity: [{ time: 12.4, drone: 4, velocity: 4.8 }],
      jerk: [{ time: 1 }],
    });
    expect(lines).toEqual([
      'Drones 2 & 3 1.2 m apart at t = 6.1 s',
      'Drone 4 at 4.8 m/s at t = 12.4 s',
      'Jerk violation at t = 1 s',
    ]);
  });

  it('caps the list and says how many more', () => {
    const many = { proximity: Array.from({ length: 8 }, (_, i) => ({ time: i, drones: [1, 2], distance: 1 })) };
    const lines = violationLines(many, 3);
    expect(lines).toHaveLength(4);
    expect(lines[3]).toBe('… and 5 more');
  });

  it('is empty for nothing', () => {
    expect(violationLines(undefined)).toEqual([]);
    expect(violationLines({ proximity: [] })).toEqual([]);
  });
});

describe('applyBlock', () => {
  const doc = mkDoc();
  const command = cmd(doc, 'set hold of scene 1 to 8 s');
  const base = { doc, command, check: { status: 'PASSED' }, checkContext: 'x', context: 'x' };

  it('allows only a PASSED check on an unchanged document and context', () => {
    expect(applyBlock(base)).toBeNull();
  });

  it.each(['FAILED', 'UNVERIFIED', 'REFUSED', 'CHECKING'])('blocks %s', (status) => {
    expect(applyBlock({ ...base, check: { status } })?.kind).toBe('check');
  });

  it('blocks a missing check or command', () => {
    expect(applyBlock({ ...base, check: null })?.kind).toBe('check');
    expect(applyBlock({ ...base, command: null })?.kind).toBe('check');
  });

  it('blocks a stale proposal with the engine reason, before looking at the check', () => {
    const changed = { ...doc, scenes: doc.scenes.map((s) => (s.id === 'c' ? { ...s, scale: 2 } : s)) };
    const b = applyBlock({ ...base, doc: changed, check: { status: 'FAILED' } });
    expect(b.kind).toBe('stale');
    expect(b.reason).toMatch(/^stale/);
  });

  it('blocks when the build inputs changed since the check', () => {
    expect(applyBlock({ ...base, context: 'y' })?.kind).toBe('context');
  });
});
