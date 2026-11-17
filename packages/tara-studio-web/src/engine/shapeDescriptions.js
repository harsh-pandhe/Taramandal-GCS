// A one-or-two-sentence description of each formation in the Shape Library (issue #92, "useful
// descriptions"), shown under the preview so a new operator knows what a shape is and what it
// does with a large fleet. Each line only says what engine/formations.js does; the numbers the
// build applies (spacing floor, site radius) are not repeated here, so they cannot go stale.

const PARAMETRIC_TEXT = {
  circle: 'A flat ring at show altitude with the drones evenly spaced. The ring widens as the fleet grows, and a fleet too big for the site is split into stacked rings.',
  grid: 'A flat, roughly square grid filled row by row. A fleet too wide for the site is split into stacked layers.',
  line: 'A single flat row, one drone after another. A fleet too long for the site is split into stacked layers.',
  heart: 'A flat heart outline with the drones evenly spaced along it.',
  sphere: 'A 3D ball with the drones spread evenly over its surface. The ball grows with the fleet to keep neighbours apart, up to a size limit.',
  vee: 'A V with the lead drone at the tip and the others alternating left and right as the arms trail away.',
};

const SVG_TEXT = {
  dandiya_sticks: 'Two crossed dandiya sticks, traced from an SVG outline.',
  diya_flame: 'A diya (oil lamp) with its flame, traced from an SVG outline.',
};

// The characters engine/formations.js can draw (its GLYPHS table, matched case-insensitively).
const DRAWABLE = /^[0-9A-FHLPU ]$/i;

const GENERIC = {
  svg: 'A shape traced from an SVG outline.',
  points: 'A formation placed from a list of points.',
  exact_points: 'A formation placed at exactly the points given.',
  mesh: 'A formation sampled from a 3D mesh.',
};

/**
 * What the formation is, in plain words.
 * @param {string} shapeType parametric | svg | text | points | exact_points | mesh
 * @param {string} shapePayload the type's own payload (a name, the text, ...)
 */
export const describeShape = (shapeType, shapePayload) => {
  if (shapeType === 'parametric') return PARAMETRIC_TEXT[shapePayload] || 'A built-in formation.';
  if (shapeType === 'svg') return SVG_TEXT[shapePayload] || GENERIC.svg;
  if (shapeType === 'text') {
    const chars = [...String(shapePayload ?? '')];
    const stands = 'standing upright and facing the audience';
    if (chars.length === 1 && DRAWABLE.test(chars[0])) {
      return `The character ${chars[0].toUpperCase()} drawn in seven-segment strokes, ${stands}.`;
    }
    const undrawable = chars.some((c) => !DRAWABLE.test(c));
    return `The text drawn in seven-segment strokes, ${stands}.${undrawable ? ` A character it cannot draw is drawn as an 8: only digits and A-F, H, L, P, U can be drawn.` : ''}`;
  }
  return GENERIC[shapeType] || 'A custom formation.';
};
