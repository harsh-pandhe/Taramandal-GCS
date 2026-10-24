// useShowBuilder.build(): refusals are reported, unexpected errors are not
// disguised, loading is always cleared, and nothing unchecked is ever produced.
// The package has no DOM testing stack, so React's state hooks are replaced by
// recorders and the hook is called as a plain function.
import { describe, it, expect, vi, beforeEach } from 'vitest';

const H = vi.hoisted(() => ({ calls: [], validate: null }));
vi.mock('react', () => ({
  useState: (init) => [init, (v) => H.calls.push(v)],
  useCallback: (fn) => fn,
}));
vi.mock('../engine/api.js', () => ({
  API_BASE: 'http://test',
  validateShow: (...a) => H.validate(...a),
}));
vi.mock('../engine/sequencer.js', async (orig) => ({
  ...(await orig()),
  computeAssignmentChain: vi.fn(),
  computeAssignmentChainLocal: vi.fn(),
  buildSequencedShow: vi.fn(),
}));

// Imported under a non-hook name: the hook only uses the mocked state recorders,
// so it is invoked as a plain function here, outside a component.
import createBuilder, { assignAndBuild } from './useShowBuilder.js';
import * as seq from '../engine/sequencer.js';

const scenes = [seq.makeScene({ id: 'a', name: 'Heart', shapePayload: 'heart' })];
const SHOW = { drones: [], _duration: 1 };

/** Runs build() and returns the ordered list of state-setter arguments. */
const runBuild = async (...args) => {
  H.calls = [];
  const { build } = createBuilder();
  const result = await build(...args);
  return { result, calls: H.calls };
};

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.spyOn(console, 'error').mockImplementation(() => {});
  seq.computeAssignmentChain.mockReset();
  seq.computeAssignmentChainLocal.mockReset();
  seq.buildSequencedShow.mockReset();
  H.validate = () => Promise.resolve(null);
});

describe('assignAndBuild', () => {
  it('a typed refusal from the chain is rethrown, not retried locally', async () => {
    const refusal = new seq.MorphRefusalError('Scene "Heart": too tall', { code: 'FORMATION' });
    seq.computeAssignmentChain.mockRejectedValue(refusal);
    await expect(assignAndBuild(scenes, 500, 4)).rejects.toBe(refusal);
    expect(seq.computeAssignmentChainLocal).not.toHaveBeenCalled();
    expect(seq.buildSequencedShow).not.toHaveBeenCalled();
  });

  it('an unreachable backend falls back to the LOCAL optimal assignment, never to identity', async () => {
    seq.computeAssignmentChain.mockRejectedValue(new TypeError('fetch failed'));
    seq.computeAssignmentChainLocal.mockReturnValue([[1, 0], [1, 0]]);
    seq.buildSequencedShow.mockReturnValue(SHOW);
    await expect(assignAndBuild(scenes, 2, '4')).resolves.toBe(SHOW);
    expect(seq.buildSequencedShow).toHaveBeenCalledWith(scenes, 2, 4, [[1, 0], [1, 0]]);
  });

  it('a refusal raised by the local solver itself propagates', async () => {
    seq.computeAssignmentChain.mockRejectedValue(new Error('network'));
    const refusal = new seq.MorphRefusalError('refused', { code: 'FORMATION' });
    seq.computeAssignmentChainLocal.mockImplementation(() => { throw refusal; });
    await expect(assignAndBuild(scenes, 500, 4)).rejects.toBe(refusal);
  });

  it('a bug in the local solver propagates unchanged', async () => {
    seq.computeAssignmentChain.mockRejectedValue(new Error('network'));
    seq.computeAssignmentChainLocal.mockImplementation(() => { throw new TypeError('boom'); });
    await expect(assignAndBuild(scenes, 5, 4)).rejects.toThrow(TypeError);
    expect(seq.buildSequencedShow).not.toHaveBeenCalled();
  });
});

describe('build() state handling', () => {
  it('success: loading goes true then false, show is set, validation requested', async () => {
    seq.computeAssignmentChain.mockResolvedValue([[0], [0]]);
    seq.buildSequencedShow.mockReturnValue(SHOW);
    const validate = vi.fn(() => Promise.resolve({ metrics: {} }));
    H.validate = validate;
    const { result, calls } = await runBuild(scenes, 1, 4);
    expect(result).toBe(SHOW);
    expect(calls[0]).toBe(true);
    expect(calls).toContain(SHOW);
    expect(calls.lastIndexOf(false)).toBeGreaterThan(calls.indexOf(true));
    expect(validate).toHaveBeenCalledWith(SHOW);
  });

  it('a typed refusal shows an actionable message, produces no show, and clears loading', async () => {
    seq.computeAssignmentChain.mockRejectedValue(
      new seq.MorphRefusalError('Scene "Heart": exceeds maximum altitude ceiling', { code: 'FORMATION' }),
    );
    const { result, calls } = await runBuild(scenes, 500, 4);
    expect(result).toBeNull();
    expect(calls).toContain('Sequence build failed: Scene "Heart": exceeds maximum altitude ceiling');
    expect(calls.at(-1)).toBe(false); // loading cleared in the terminal path
    expect(calls).not.toContain(SHOW);
    expect(console.error).not.toHaveBeenCalled();
  });

  it('an unexpected error is logged, reported as a bug, produces no show, and clears loading', async () => {
    seq.computeAssignmentChain.mockResolvedValue([[0, 0], [0, 1]]);
    const bug = new TypeError('targetIdx entry is not a permutation of the fleet.');
    seq.buildSequencedShow.mockImplementation(() => { throw bug; });
    const { result, calls } = await runBuild(scenes, 2, 4);
    expect(result).toBeNull();
    expect(console.error).toHaveBeenCalledWith(bug);
    const msg = calls.find((c) => typeof c === 'string' && c.startsWith('Unexpected error'));
    expect(msg).toMatch(/TypeError: targetIdx entry is not a permutation/);
    expect(msg).toMatch(/not a safety refusal/);
    expect(calls.at(-1)).toBe(false);
    expect(calls.some((c) => c && c.drones)).toBe(false); // nothing built, no nominal fallback
  });

  it('an unexpected error escaping the chain solver also clears loading (the #147 stuck-loading case)', async () => {
    seq.computeAssignmentChain.mockRejectedValue(new Error('network'));
    seq.computeAssignmentChainLocal.mockImplementation(() => { throw new RangeError('bad formation index'); });
    const { result, calls } = await runBuild(scenes, 5, 4);
    expect(result).toBeNull();
    expect(calls.at(-1)).toBe(false);
    expect(calls.find((c) => typeof c === 'string' && c.startsWith('Unexpected error'))).toMatch(/RangeError/);
  });

  it('no scenes: reports and never sets loading', async () => {
    const { result, calls } = await runBuild([], 5, 4);
    expect(result).toBeNull();
    expect(calls).toEqual(['Add at least one scene.']);
  });
});
