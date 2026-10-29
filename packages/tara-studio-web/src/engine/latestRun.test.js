import { describe, it, expect } from 'vitest';
import { createLatestRun } from './latestRun.js';

// Models useShowBuilder's background validation: each build begins a run, the
// run's result may only be applied (and the shared "validating" flag only
// cleared) while it is still the latest.
const runValidation = (gate, state, label, resolveWith) => {
  const id = gate.begin();
  state.validating = true;
  return resolveWith.then((v) => {
    if (!gate.isCurrent(id)) return;
    state.validation = `${label}:${v}`;
  }).finally(() => { if (gate.isCurrent(id)) state.validating = false; });
};

const deferred = () => {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
};

describe('createLatestRun', () => {
  it('only the most recent run is current', () => {
    const gate = createLatestRun();
    const a = gate.begin();
    expect(gate.isCurrent(a)).toBe(true);
    const b = gate.begin();
    expect(gate.isCurrent(a)).toBe(false);
    expect(gate.isCurrent(b)).toBe(true);
  });

  it('invalidate() supersedes the current run without starting a new one', () => {
    const gate = createLatestRun();
    const a = gate.begin();
    gate.invalidate();
    expect(gate.isCurrent(a)).toBe(false);
  });

  it('an older validation that resolves late cannot overwrite the newer show or clear its indicator', async () => {
    const gate = createLatestRun();
    const state = { validation: null, validating: false };
    const first = deferred();
    const second = deferred();

    const p1 = runValidation(gate, state, 'show1', first.promise);   // build #1
    const p2 = runValidation(gate, state, 'show2', second.promise);  // build #2 while #1 still runs

    first.resolve('PASSED');   // the stale validation for show 1 finishes first
    await p1;
    expect(state.validation).toBeNull();      // not applied to show 2
    expect(state.validating).toBe(true);      // indicator still on: show 2 is still validating

    second.resolve('FAILED');
    await p2;
    expect(state.validation).toBe('show2:FAILED');
    expect(state.validating).toBe(false);
  });

  it('a validation in flight when the show is replaced from outside is dropped', async () => {
    const gate = createLatestRun();
    const state = { validation: 'loaded-project', validating: false };
    const pending = deferred();
    const p = runValidation(gate, state, 'old', pending.promise);
    gate.invalidate();            // a project was loaded over the show
    state.validating = false;     // what the external setters do
    pending.resolve('PASSED');
    await p;
    expect(state.validation).toBe('loaded-project');
    expect(state.validating).toBe(false);
  });
});
