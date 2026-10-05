import { describe, it, expect } from 'vitest';
import { makeScene } from './sequencer.js';
import {
  MAX_VERSIONS, addVersion, findVersion, hasVersionOf, restoreVersion, versionsOf,
} from './projectVersions.js';

const state = (names, extra = {}) => ({
  scenes: names.map((n) => makeScene({ id: `id-${n}`, name: n })),
  rate: 4, homeLat: 12.9, homeLon: 77.5,
  dronesList: [{ id: 0, name: 'Alpha' }],
  choreoData: { drones: ['big'] }, validation: { status: 'PASSED' },
  ...extra,
});
const project = (names) => ({ id: 'p1', name: 'Show', ...state(names) });

describe('project versions', () => {
  it('treats a project saved before versions existed as having none', () => {
    expect(versionsOf(project(['A']))).toEqual([]);
    expect(versionsOf(null)).toEqual([]);
    expect(versionsOf({ versions: 'corrupt' })).toEqual([]);
  });

  it('adds a timestamped version of the editable state, newest first', () => {
    let p = addVersion(project(['A']), state(['A']), Date.UTC(2026, 9, 3, 10, 0, 0));
    p = addVersion(p, state(['A', 'B']), Date.UTC(2026, 9, 3, 10, 5, 0));
    const versions = versionsOf(p);
    expect(versions.map((v) => v.scenes.length)).toEqual([2, 1]);
    expect(versions[0].savedAt).toBe('2026-10-03T10:05:00.000Z');
    expect(versions[0].label).toBe('Saved');
    expect(new Set(versions.map((v) => v.id)).size).toBe(2);
  });

  it('keeps the built trajectory out of versions', () => {
    const [v] = versionsOf(addVersion(project(['A']), state(['A']), 1));
    expect(v).not.toHaveProperty('choreoData');
    expect(v).not.toHaveProperty('validation');
  });

  it('does not add a duplicate when nothing changed since the last version', () => {
    const p1 = addVersion(project(['A']), state(['A']), 1);
    expect(addVersion(p1, state(['A']), 2)).toBe(p1);
    // A rebuilt trajectory alone is not a new version of the show.
    expect(addVersion(p1, state(['A'], { choreoData: { drones: ['other'] } }), 3)).toBe(p1);
    expect(versionsOf(addVersion(p1, state(['A'], { rate: 8 }), 4))).toHaveLength(2);
  });

  it('caps the list, dropping the oldest', () => {
    let p = project(['A']);
    for (let i = 0; i < MAX_VERSIONS + 5; i++) p = addVersion(p, state(['A'], { rate: i + 1 }), i);
    const versions = versionsOf(p);
    expect(versions).toHaveLength(MAX_VERSIONS);
    expect(versions[0].rate).toBe(MAX_VERSIONS + 5);
    expect(versions[versions.length - 1].rate).toBe(6);
  });

  it('never mutates the project it is given', () => {
    const p = addVersion(project(['A']), state(['A']), 1);
    const before = JSON.stringify(p);
    addVersion(p, state(['A', 'B']), 2);
    restoreVersion(p, versionsOf(p)[0]);
    expect(JSON.stringify(p)).toBe(before);
  });

  it('restores a prior point and clears the stale build', () => {
    let p = addVersion(project(['A']), state(['A']), 1);
    p = { ...addVersion(p, state(['A', 'B', 'C'], { rate: 8 }), 2), ...state(['A', 'B', 'C'], { rate: 8 }) };
    const oldest = versionsOf(p)[1];

    const restored = restoreVersion(p, findVersion(p, oldest.id));
    expect(restored.scenes.map((s) => s.name)).toEqual(['A']);
    expect(restored.rate).toBe(4);
    expect(restored.choreoData).toBeNull();
    expect(restored.validation).toBeNull();
    // Restoring keeps the history: the newer save is still there to go back to.
    expect(versionsOf(restored)).toHaveLength(2);
    expect(restored.name).toBe('Show');
  });

  it('hasVersionOf finds a matching state anywhere in the list', () => {
    let p = addVersion(project(['A']), state(['A']), 1);
    p = addVersion(p, state(['A', 'B']), 2);
    expect(hasVersionOf(p, state(['A']))).toBe(true);
    expect(hasVersionOf(p, state(['A', 'B']))).toBe(true);
    expect(hasVersionOf(p, state(['Z']))).toBe(false);
    expect(hasVersionOf(project(['A']), state(['A']))).toBe(false);
  });

  it('findVersion returns null for an unknown id', () => {
    expect(findVersion(addVersion(project(['A']), state(['A']), 1), 'nope')).toBeNull();
  });
});
