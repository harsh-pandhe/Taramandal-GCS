import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import ShapeLibraryModal from './ShapeLibraryModal.jsx';

// Rendered to static markup, same as SafetyPanel.test.jsx: the package has no
// DOM testing stack, but renderToStaticMarkup still runs every useMemo during
// render, so a refusal that isn't caught crashes this exactly as it would a
// real page (issue #141).
const render = (scene, numDrones) => renderToStaticMarkup(
  <ShapeLibraryModal scene={scene} numDrones={numDrones} onApply={() => {}} onClose={() => {}} />,
);

describe('ShapeLibraryModal', () => {
  it('renders a feasible shape/scale/fleet-size combination normally', () => {
    const html = render({ shapeType: 'parametric', shapePayload: 'circle', scale: 1.0 }, 24);
    expect(html).toContain('shape-preview');
    expect(html).not.toContain('shape-preview empty');
  });

  it('shows an inline refusal instead of crashing when the inspector preview is infeasible', () => {
    // buildFormation('parametric', 'line', 100, 20.0) throws "exceeds maximum
    // altitude ceiling" (see formations.test.js) — a real refusal the scale
    // slider can reach live, not a bug.
    const scene = { shapeType: 'parametric', shapePayload: 'line', scale: 20.0 };
    let html;
    expect(() => { html = render(scene, 100); }).not.toThrow();
    expect(html).toContain('shape-preview empty');
    expect(html).toContain('exceeds maximum altitude ceiling');
  });
});
