import { describe, it, expect, vi } from 'vitest';
import { checkProposal } from './proposalCheck.js';
import { ShowRefusalError } from './showErrors.js';

const doc = { scenes: [{ id: 'a' }], dronesList: [] };
const REPORT = (status, extra = {}) => ({
  status, summary: `${status} summary`, metrics: { min_distance_m: 2 },
  violations: { proximity: [], velocity: [], acceleration: [] }, ...extra,
});
const run = (over = {}) => checkProposal({
  afterDoc: doc, build: async () => ({ show: 1 }), validate: async () => REPORT('PASSED'), ...over,
});

describe('checkProposal', () => {
  it('PASSED: builds the after document and validates the built show', async () => {
    const build = vi.fn(async () => ({ show: 1 }));
    const validate = vi.fn(async () => REPORT('PASSED'));
    const r = await run({ build, validate });
    expect(build).toHaveBeenCalledWith(doc);
    expect(validate).toHaveBeenCalledWith({ show: 1 });
    expect(r).toMatchObject({ status: 'PASSED', summary: 'PASSED summary' });
  });

  // The app's own gate (engine/exportGate.js) calls these two FAILED; the panel must agree.
  it('FAILED when the hard limits pass but the show is not flight-certified (feasibility_certified === false)', async () => {
    const r = await run({ validate: async () => REPORT('PASSED', { feasibility_certified: false }) });
    expect(r.status).toBe('FAILED');
    expect(r.summary).toMatch(/not flight-certified/);
  });

  it('FAILED when a PASSED report still lists jerk violations, and keeps them', async () => {
    const violations = { proximity: [], jerk: [{ drone: 1, time: 10, jerk: 21, limit: 15 }] };
    const r = await run({ validate: async () => REPORT('PASSED', { violations }) });
    expect(r.status).toBe('FAILED');
    expect(r.violations).toEqual(violations);
  });

  it('still PASSED when the report says nothing about feasibility (older backend)', async () => {
    const r = await run({ validate: async () => REPORT('PASSED') });
    expect(r.status).toBe('PASSED');
  });

  it('FAILED: carries the violations', async () => {
    const violations = { proximity: [{ time: 6.1, drones: [2, 3], distance: 1.2 }], velocity: [], acceleration: [] };
    const r = await run({ validate: async () => REPORT('FAILED', { violations }) });
    expect(r.status).toBe('FAILED');
    expect(r.violations).toEqual(violations);
  });

  it('UNVERIFIED when the service is down, without throwing', async () => {
    const r = await run({ validate: async () => { throw new Error('offline'); } });
    expect(r.status).toBe('UNVERIFIED');
    expect(r.summary).toMatch(/unavailable: offline/);
  });

  it('UNVERIFIED for a response with no metrics, or an unknown status', async () => {
    expect((await run({ validate: async () => ({ status: 'PASSED' }) })).status).toBe('UNVERIFIED');
    expect((await run({ validate: async () => null })).status).toBe('UNVERIFIED');
    expect((await run({ validate: async () => REPORT('MAYBE') })).status).toBe('UNVERIFIED');
  });

  it('REFUSED when the build throws ShowRefusalError, and never validates', async () => {
    const validate = vi.fn();
    const r = await run({ build: async () => { throw new ShowRefusalError('too dense'); }, validate });
    expect(r).toEqual({ status: 'REFUSED', summary: 'too dense', violations: {}, kind: null });
    expect(validate).not.toHaveBeenCalled();
  });

  it('any other build error is UNVERIFIED, never a throw', async () => {
    const r = await run({ build: () => { throw new TypeError('boom'); } });
    expect(r.status).toBe('UNVERIFIED');
    expect(r.summary).toMatch(/boom/);
  });
});

// `kind` is exportGate's own word for the result (issue #94, the audit log records it).
describe('checkProposal kind', () => {
  it('PASSED -> open', async () => {
    expect(await run()).toMatchObject({ status: 'PASSED', kind: 'open' });
  });

  it('FAILED -> failed, for a FAILED report and for a PASSED one that is not flight-certified', async () => {
    expect(await run({ validate: async () => REPORT('FAILED') })).toMatchObject({ status: 'FAILED', kind: 'failed' });
    const r = await run({ validate: async () => REPORT('PASSED', { feasibility_certified: false }) });
    expect(r).toMatchObject({ status: 'FAILED', kind: 'failed' });
  });

  it('REFUSED -> null (no gate was consulted)', async () => {
    const r = await run({ build: async () => { throw new ShowRefusalError('too dense'); } });
    expect(r.kind).toBeNull();
  });

  it('UNVERIFIED -> unverified on every path that returns it', async () => {
    const results = [
      await run({ validate: async () => { throw new Error('offline'); } }),
      await run({ validate: async () => null }),
      await run({ validate: async () => ({ status: 'PASSED' }) }),
      await run({ build: () => { throw new TypeError('boom'); } }),
      await run({ validate: async () => REPORT('MAYBE') }),
    ];
    for (const r of results) expect(r).toMatchObject({ status: 'UNVERIFIED', kind: 'unverified' });
  });

  it('only ever returns none, open, failed, unverified or null, and keeps its other fields', async () => {
    const all = [
      await run(),
      await run({ validate: async () => REPORT('FAILED') }),
      await run({ validate: async () => REPORT('MAYBE') }),
      await run({ validate: async () => { throw new Error('x'); } }),
      await run({ build: async () => { throw new ShowRefusalError('r'); } }),
    ];
    for (const r of all) {
      expect(['none', 'open', 'failed', 'unverified', null]).toContain(r.kind);
      expect(Object.keys(r).sort()).toEqual(['kind', 'status', 'summary', 'violations']);
    }
  });
});
