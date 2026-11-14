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
    expect(r).toEqual({ status: 'REFUSED', summary: 'too dense', violations: {} });
    expect(validate).not.toHaveBeenCalled();
  });

  it('any other build error is UNVERIFIED, never a throw', async () => {
    const r = await run({ build: () => { throw new TypeError('boom'); } });
    expect(r.status).toBe('UNVERIFIED');
    expect(r.summary).toMatch(/boom/);
  });
});
