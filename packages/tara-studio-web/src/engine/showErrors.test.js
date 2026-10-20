import { describe, expect, it } from 'vitest';
import { ShowRefusalError } from './showErrors.js';

// The contract every narrow catch (App.jsx's predicted-duration/timeline
// useMemo, ShapeLibraryModal's preview, useShowBuilder's build()) relies on:
// a refusal is an Error (so `.message` and stack traces still work) but is
// distinguishable from one, so a genuine bug is never mistaken for a refusal.
describe('ShowRefusalError', () => {
  it('is an Error with the message intact', () => {
    const e = new ShowRefusalError('too close');
    expect(e).toBeInstanceOf(Error);
    expect(e.message).toBe('too close');
    expect(e.name).toBe('ShowRefusalError');
  });

  it('is what instanceof-based catches are built to single out', () => {
    expect(new ShowRefusalError('x') instanceof ShowRefusalError).toBe(true);
    expect(new Error('x') instanceof ShowRefusalError).toBe(false);
    expect(new TypeError('x') instanceof ShowRefusalError).toBe(false);
  });
});
