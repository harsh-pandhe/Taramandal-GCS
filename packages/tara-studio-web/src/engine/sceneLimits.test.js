import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { SCENE_LIMITS } from './sceneLimits.js';

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8');

// Finds `label="<label>" ... min={a} max={b}` on one JSX line.
const uiRange = (source, label) => {
  const m = source.match(new RegExp(`label="${label}"[^\\n]*?min=\\{([\\d.]+)\\}\\s+max=\\{([\\d.]+)\\}`));
  if (!m) throw new Error(`no min/max found for field "${label}"`);
  return { min: Number(m[1]), max: Number(m[2]) };
};

describe('scene limits match the UI', () => {
  const card = read('../components/sequencer/SceneCard.jsx');
  const modal = read('../components/modals/ShapeLibraryModal.jsx');

  it('hold', () => expect(SCENE_LIMITS.holdDurationSeconds).toEqual(uiRange(card, 'Hold')));
  it('transition (morph in)', () => expect(SCENE_LIMITS.transitionDurationSeconds).toEqual(uiRange(card, 'Morph in')));
  it('layer gap', () => expect(SCENE_LIMITS.transitLayerGapMeters).toEqual(uiRange(card, 'Layer gap')));
  it('scale', () => expect(SCENE_LIMITS.scale).toEqual(uiRange(modal, 'Scale')));

  it('states the agreed numbers', () => {
    expect(SCENE_LIMITS.holdDurationSeconds).toEqual({ min: 0, max: 120 });
    expect(SCENE_LIMITS.transitionDurationSeconds).toEqual({ min: 0, max: 60 });
    expect(SCENE_LIMITS.transitLayerGapMeters).toEqual({ min: 0.5, max: 6 });
    expect(SCENE_LIMITS.scale).toEqual({ min: 0.25, max: 3 });
  });
});
