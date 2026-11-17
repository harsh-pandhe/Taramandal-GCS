import { describe, expect, it } from 'vitest';
import { describeShape } from './shapeDescriptions.js';
import { SHAPE_LIBRARY, buildFormation } from './formations.js';

describe('describeShape (issue #92)', () => {
  it('describes every entry of the Shape Library, each one differently', () => {
    const texts = SHAPE_LIBRARY.map((e) => describeShape(e.shapeType, e.shapePayload));
    for (const t of texts) {
      expect(t.length).toBeGreaterThan(20);
      expect(t).not.toMatch(/^A (built-in|custom) formation\.$/); // no entry falls through to the generic line
    }
    // The ten digits share one sentence shape but name their own digit.
    const parametricAndSvg = SHAPE_LIBRARY.filter((e) => e.shapeType !== 'text')
      .map((e) => describeShape(e.shapeType, e.shapePayload));
    expect(new Set(parametricAndSvg).size).toBe(parametricAndSvg.length);
    const digits = SHAPE_LIBRARY.filter((e) => e.shapeType === 'text').map((e) => describeShape(e.shapeType, e.shapePayload));
    expect(new Set(digits).size).toBe(10);
  });

  it('says what a custom text really does: unsupported characters are drawn as an 8', () => {
    // This is the behaviour the sentence relies on: 'Z' has no glyph, so it is the same cloud as '8'.
    const z = buildFormation('text', 'Z', 12, 1);
    const eight = buildFormation('text', '8', 12, 1);
    expect(z).toEqual(eight);
    expect(describeShape('text', 'Z')).toMatch(/drawn as an 8/);
    expect(describeShape('text', 'TARA')).toMatch(/drawn as an 8/);
    // Supported text makes no such warning, and lower case is accepted like upper case.
    expect(describeShape('text', 'HELP')).not.toMatch(/drawn as an 8/);
    expect(describeShape('text', 'a')).toMatch(/character A/);
  });

  it('does not throw on a missing payload or an unknown type', () => {
    expect(describeShape('text', undefined)).toBeTruthy();
    expect(describeShape('mystery', 'x')).toBe('A custom formation.');
    expect(describeShape('parametric', 'nope')).toBe('A built-in formation.');
  });
});
