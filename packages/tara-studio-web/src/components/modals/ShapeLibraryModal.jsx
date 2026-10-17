import { useState, useEffect, useMemo } from 'react';
import { X, Check } from 'lucide-react';
import { SHAPE_LIBRARY, buildFormation } from '../../engine/formations.js';
import { Slider } from '../common/index.jsx';

// Point count used for the library tiles, independent of the real fleet size —
// enough samples that a glyph or curve is recognisable at thumbnail size.
const TILE_SAMPLE_POINTS = 34;

// Projects a formation to a 2D preview.
//
// Formations live in NED (z negative = up). Text stands upright in the Y/alt
// plane, but the parametric shapes are laid out flat in X/Y at a constant
// altitude — projecting those onto Y/alt would draw every one of them as a
// featureless horizontal line. So the preview picks whichever pair of axes the
// shape actually varies in, and shows it face-on.
const PreviewBox = ({ shapeType, shapePayload, scale, n }) => {
  // A formation refused at this fleet size has no preview; say so rather than
  // throwing out of render and taking the whole Studio down.
  const pts = useMemo(() => {
    try {
      return buildFormation(shapeType, shapePayload, n, scale);
    } catch {
      return null;
    }
  }, [shapeType, shapePayload, scale, n]);

  if (pts === null) {
    return <div className="shape-preview empty" title={`Not possible at ${n} drones`}>Not possible at {n} drones</div>;
  }
  if (!pts.length) return <div className="shape-preview empty" />;

  const extent = (vals) => Math.max(...vals) - Math.min(...vals);
  const xs = pts.map((p) => p.x);
  const ys = pts.map((p) => p.y);
  const ups = pts.map((p) => -p.z); // flip NED so up is up

  // Horizontal axis is always the fleet's Y (the audience's left-to-right).
  // The vertical axis is altitude if the shape has meaningful height, else X.
  const useAltitude = extent(ups) > Math.max(extent(xs), 1e-6);
  const vert = useAltitude ? ups : xs;

  const minH = Math.min(...ys), maxH = Math.max(...ys);
  const minV = Math.min(...vert), maxV = Math.max(...vert);
  // One span for both axes so a circle stays round rather than being stretched.
  const span = Math.max(maxH - minH, maxV - minV, 1e-6) * 1.15;
  const ch = (minH + maxH) / 2, cv = (minV + maxV) / 2;

  return (
    <svg className="shape-preview" viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet">
      <rect x="0" y="0" width="100" height="100" className="shape-preview-bg" />
      {pts.map((p, i) => (
        <circle
          key={i}
          cx={50 + ((p.y - ch) / span) * 100}
          cy={50 - ((vert[i] - cv) / span) * 100}
          r="1.6"
        />
      ))}
    </svg>
  );
};

/**
 * Shape picker with a live preview, so a designer can see what a formation and
 * scale actually look like before committing the scene to a rebuild.
 *
 * Edits are staged locally and only applied on "Use shape" — picking through
 * the library shouldn't invalidate the built show until the user commits.
 */
export default function ShapeLibraryModal({ scene, numDrones, onApply, onClose }) {
  const [shapeType, setShapeType] = useState(scene.shapeType);
  const [shapePayload, setShapePayload] = useState(scene.shapePayload);
  const [scale, setScale] = useState(scene.scale);
  const [customText, setCustomText] = useState(
    scene.shapeType === 'text' ? scene.shapePayload : '',
  );

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const isSelected = (entry) =>
    entry.shapeType === shapeType && entry.shapePayload === shapePayload;

  return (
    <div className="modal-backdrop" onPointerDown={onClose}>
      <div
        className="modal-card shape-library"
        onPointerDown={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Shape library"
      >
        <div className="modal-head">
          <h3>Shape Library</h3>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>

        <div className="shape-library-body">
          <div className="shape-grid">
            {SHAPE_LIBRARY.map((entry) => (
              <button
                type="button"
                key={`${entry.shapeType}:${entry.shapePayload}`}
                className={`shape-tile${isSelected(entry) ? ' selected' : ''}`}
                onClick={() => {
                  setShapeType(entry.shapeType);
                  setShapePayload(entry.shapePayload);
                }}
              >
                {/* Tiles preview the shape itself, at a fixed density — sampling
                    them at the real fleet size would render a 3-drone show's
                    library as a grid of near-identical dots. The inspector
                    below shows the true fleet-size rendering. */}
                <PreviewBox
                  shapeType={entry.shapeType}
                  shapePayload={entry.shapePayload}
                  scale={1}
                  n={TILE_SAMPLE_POINTS}
                />
                <span>{entry.label}</span>
              </button>
            ))}
          </div>

          <div className="shape-inspector">
            <span className="shape-inspector-title">Preview</span>
            <PreviewBox
              shapeType={shapeType}
              shapePayload={shapePayload}
              scale={scale}
              n={numDrones}
            />

            <Slider
              label="Scale" min={0.25} max={3} step={0.05} value={scale}
              onChange={setScale} format={(v) => `×${v.toFixed(2)}`}
            />

            <label className="ui-field">
              <span className="ui-field-label">Custom text</span>
              <input
                value={customText}
                maxLength={8}
                placeholder="e.g. 5 or TARA"
                onChange={(e) => {
                  const v = e.target.value;
                  setCustomText(v);
                  if (v.trim()) {
                    setShapeType('text');
                    setShapePayload(v.trim());
                  }
                }}
              />
            </label>
            <p className="shape-hint">
              Digits and A–F, H, L, P, U render as seven-segment glyphs standing
              upright, facing the audience.
            </p>

            <button
              type="button"
              className="btn-primary"
              onClick={() => onApply({ shapeType, shapePayload, scale })}
            >
              <Check size={14} /> Use shape
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
