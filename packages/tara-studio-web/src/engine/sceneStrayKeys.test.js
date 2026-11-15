import { describe, expect, it } from 'vitest';
import { stripStraySceneKeys, sceneSignature } from './sceneValidation.js';
import { makeScene } from './sequencer.js';

// Issue #282, item 6: the old "Snap scenes to beats" patched a `hold` that scenes do not have, so a
// project saved afterwards can carry `hold: null` (a NaN saved as JSON) in its scenes.
describe('stripStraySceneKeys', () => {
  it('returns the very same array when no scene has a stray key (so a clean project is not "changed")', () => {
    const scenes = [makeScene({ id: 'a' }), makeScene({ id: 'b' })];
    expect(stripStraySceneKeys(scenes)).toBe(scenes);
  });

  it('removes `hold` and leaves every real field alone', () => {
    const scenes = [makeScene({ id: 'a', hold: null, holdDurationSeconds: 7 }), makeScene({ id: 'b' })];
    const out = stripStraySceneKeys(scenes);
    expect(out).not.toBe(scenes);
    expect('hold' in out[0]).toBe(false);
    expect(out[0]).toMatchObject({ id: 'a', holdDurationSeconds: 7, transitionDurationSeconds: 4, scale: 1 });
    expect(out[1]).toBe(scenes[1]); // a clean scene is not copied
    expect('hold' in scenes[0]).toBe(true); // the input is not mutated
  });

  it('makes the signature match a scene that never had the key', () => {
    const clean = makeScene({ id: 'a' });
    const dirty = { ...clean, hold: null };
    expect(sceneSignature({ scenes: stripStraySceneKeys([dirty]) })).toBe(sceneSignature({ scenes: [clean] }));
  });

  it('tolerates input that is not a list of scenes', () => {
    expect(stripStraySceneKeys(undefined)).toBeUndefined();
    expect(stripStraySceneKeys(null)).toBeNull();
    const odd = [null, makeScene({ id: 'a' })];
    expect(stripStraySceneKeys(odd)).toBe(odd);
  });
});
