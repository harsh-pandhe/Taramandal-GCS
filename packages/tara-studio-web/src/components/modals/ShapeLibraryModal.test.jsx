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

  it('describes the selected shape in words, and the description follows a custom shape (issue #92)', () => {
    const ring = render({ shapeType: 'parametric', shapePayload: 'circle', scale: 1.0 }, 24);
    expect(ring).toContain('data-testid="shape-description"');
    expect(ring).toContain('A flat ring at show altitude');
    const heart = render({ shapeType: 'parametric', shapePayload: 'heart', scale: 1.0 }, 24);
    expect(heart).toContain('A flat heart outline');
    expect(heart).not.toContain('A flat ring');
    const digit = render({ shapeType: 'text', shapePayload: '7', scale: 1.0 }, 12);
    expect(digit).toContain('The character 7 drawn in seven-segment strokes');
  });

  it('keeps the description when the preview is refused', () => {
    const html = render({ shapeType: 'parametric', shapePayload: 'line', scale: 20.0 }, 100);
    expect(html).toContain('shape-preview empty');
    expect(html).toContain('A single flat row');
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
