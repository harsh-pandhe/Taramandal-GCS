import { useState, useEffect, useRef, useCallback } from 'react';
import { X, Upload, Check, RefreshCw, AlertCircle, Image as ImageIcon } from 'lucide-react';
import { convertRasterToFormation } from '../../engine/api.js';
import { Slider } from '../common/index.jsx';

/**
 * Modal to upload PNG/JPG/WebP artwork, preview extracted formation points,
 * and insert as a deterministic drone scene into the show (issue #90).
 */
export default function RasterImportModal({ numDrones = 35, onInsertScene, onClose }) {
  const [imageFile, setImageFile] = useState(null);
  const [imageBase64, setImageBase64] = useState('');
  const [imageMeta, setImageMeta] = useState(null);

  const [targetDrones, setTargetDrones] = useState(numDrones);
  const [scale, setScale] = useState(20.0);
  const [altitude, setAltitude] = useState(15.0);
  const [threshold, setThreshold] = useState(128);
  const [useAutoThreshold, setUseAutoThreshold] = useState(true);

  const [converting, setConverting] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');

  const fileInputRef = useRef(null);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const handleFile = useCallback((file) => {
    if (!file) return;
    setError('');
    setResult(null);
    setImageFile(file);

    const reader = new FileReader();
    reader.onload = (e) => {
      const dataUrl = e.target.result;
      const b64 = typeof dataUrl === 'string' ? dataUrl.split(',')[1] : '';
      setImageBase64(b64);

      // Measure dimensions
      const img = new Image();
      img.onload = () => {
        setImageMeta({
          name: file.name,
          width: img.width,
          height: img.height,
          sizeKb: Math.round(file.size / 1024),
          dataUrl,
        });
      };
      img.src = dataUrl;
    };
    reader.readAsDataURL(file);
  }, []);

  const doConvert = useCallback(async () => {
    if (!imageBase64) return;
    setConverting(true);
    setError('');
    try {
      const res = await convertRasterToFormation({
        imageB64: imageBase64,
        nDrones: Number(targetDrones),
        scale: Number(scale),
        altitude: Number(altitude),
        threshold: useAutoThreshold ? null : Number(threshold),
        minSeparationM: 2.0,
        siteRadiusM: 30.0,
      });
      setResult(res);
    } catch (err) {
      setError(err?.message || 'Failed to extract formation from image.');
    } finally {
      setConverting(false);
    }
  }, [imageBase64, targetDrones, scale, altitude, threshold, useAutoThreshold]);

  // Auto-convert on new image upload
  useEffect(() => {
    if (imageBase64) {
      doConvert();
    }
  }, [imageBase64]);

  const handleInsert = () => {
    if (!result?.points?.length) return;
    const sceneName = imageFile?.name
      ? imageFile.name.replace(/\.[^/.]+$/, '').slice(0, 20)
      : 'Raster Formation';

    onInsertScene({
      name: sceneName,
      shapeType: 'points',
      shapePayload: result.points,
      scale: 1.0,
      hold: 6.0,
      transition: 4.0,
    });
    onClose();
  };

  // Preview bounds
  const points = result?.points || [];
  const minX = points.length ? Math.min(...points.map((p) => p.x)) : -15;
  const maxX = points.length ? Math.max(...points.map((p) => p.x)) : 15;
  const minY = points.length ? Math.min(...points.map((p) => p.y)) : -15;
  const maxY = points.length ? Math.max(...points.map((p) => p.y)) : 15;
  const span = Math.max(maxX - minX, maxY - minY, 10) * 1.2;
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;

  return (
    <div className="modal-backdrop" onPointerDown={onClose}>
      <div
        className="modal-card raster-modal"
        onPointerDown={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Raster Image Formation Importer"
      >
        <div className="modal-head">
          <div className="flex-row items-center gap-2">
            <ImageIcon size={18} className="text-accent" />
            <h3>Image to Formation Converter</h3>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>

        <div className="raster-modal-body">
          {/* Left panel: Upload & parameters */}
          <div className="raster-controls">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              className="visually-hidden"
              onChange={(e) => {
                if (e.target.files?.[0]) handleFile(e.target.files[0]);
                e.target.value = '';
              }}
            />

            {!imageMeta ? (
              <div
                className="raster-dropzone"
                onClick={() => fileInputRef.current?.click()}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.preventDefault();
                  if (e.dataTransfer.files?.[0]) handleFile(e.dataTransfer.files[0]);
                }}
              >
                <Upload size={28} className="text-dim" />
                <span className="raster-drop-text">Click or drop image (PNG, JPG, WebP)</span>
                <span className="raster-drop-hint">Max 5MB • Transparent or high contrast recommended</span>
              </div>
            ) : (
              <div className="raster-source-card">
                <div className="raster-thumb-wrap">
                  <img src={imageMeta.dataUrl} alt="Source" className="raster-thumb" />
                </div>
                <div className="raster-source-meta">
                  <span className="mono font-semibold truncate" title={imageMeta.name}>{imageMeta.name}</span>
                  <span className="text-xs text-dim">
                    {imageMeta.width}×{imageMeta.height} px • {imageMeta.sizeKb} KB
                  </span>
                  <button
                    type="button"
                    className="btn-ghost btn-tiny text-xs mt-1"
                    onClick={() => fileInputRef.current?.click()}
                  >
                    Change Image
                  </button>
                </div>
              </div>
            )}

            <div className="raster-param-group">
              <label className="ui-field">
                <span className="ui-field-label">Target Fleet Drones (N)</span>
                <input
                  type="number"
                  min={1}
                  max={500}
                  value={targetDrones}
                  onChange={(e) => setTargetDrones(Math.max(1, parseInt(e.target.value, 10) || 1))}
                />
              </label>

              <Slider
                label="Formation Scale"
                min={5.0}
                max={40.0}
                step={1.0}
                value={scale}
                onChange={setScale}
                format={(v) => `${v.toFixed(1)} m`}
              />

              <Slider
                label="Flight Altitude"
                min={5.0}
                max={30.0}
                step={1.0}
                value={altitude}
                onChange={setAltitude}
                format={(v) => `${v.toFixed(1)} m`}
              />

              <div className="threshold-toggle-row">
                <label className="checkbox-label">
                  <input
                    type="checkbox"
                    checked={useAutoThreshold}
                    onChange={(e) => setUseAutoThreshold(e.target.checked)}
                  />
                  <span>Auto Otsu Threshold</span>
                </label>
              </div>

              {!useAutoThreshold && (
                <Slider
                  label="Manual Edge Threshold"
                  min={10}
                  max={245}
                  step={5}
                  value={threshold}
                  onChange={setThreshold}
                  format={(v) => `${v}`}
                />
              )}

              <button
                type="button"
                className="btn-secondary btn-inline mt-2"
                onClick={doConvert}
                disabled={!imageBase64 || converting}
              >
                <RefreshCw size={13} className={converting ? 'animate-spin' : ''} />
                {converting ? 'Extracting Points…' : 'Re-extract Formation'}
              </button>
            </div>
          </div>

          {/* Right panel: Live point formation preview */}
          <div className="raster-preview-pane">
            <div className="raster-preview-head">
              <span className="shape-inspector-title">Extracted Formation Preview</span>
              {result && (
                <span className="mono text-xs text-accent">
                  {points.length} Drones
                </span>
              )}
            </div>

            <div className="raster-canvas-wrap">
              <svg className="raster-preview-svg" viewBox="0 0 100 100" preserveAspectRatio="xMidYMid meet">
                <rect x="0" y="0" width="100" height="100" className="shape-preview-bg" />
                {/* 30m Site Boundary Circle */}
                <circle cx="50" cy="50" r="45" fill="none" stroke="rgba(255,255,255,0.08)" strokeDasharray="2,2" />
                <line x1="0" y1="50" x2="100" y2="50" stroke="rgba(255,255,255,0.06)" />
                <line x1="50" y1="0" x2="50" y2="100" stroke="rgba(255,255,255,0.06)" />

                {points.map((p, i) => (
                  <circle
                    key={i}
                    cx={50 + ((p.y - cy) / span) * 100}
                    cy={50 - ((p.x - cx) / span) * 100}
                    r="1.8"
                    fill="var(--accent)"
                  />
                ))}
              </svg>

              {converting && (
                <div className="raster-overlay-status">
                  <RefreshCw size={24} className="animate-spin text-accent" />
                  <span>Processing contours…</span>
                </div>
              )}

              {!imageBase64 && !converting && (
                <div className="raster-overlay-status text-dim">
                  <span>Upload an image to preview extracted drone coordinates</span>
                </div>
              )}
            </div>

            {error && (
              <div className="access-error-msg flex-row items-center gap-2 mt-2">
                <AlertCircle size={14} />
                <span>{error}</span>
              </div>
            )}

            {result && (
              <div className="raster-diagnostics">
                <div className="diag-item">
                  <span className="diag-label">Min Separation:</span>
                  <span className={`diag-val ${(result.min_separation_m || 2.0) >= 2.0 ? 'text-success' : 'text-danger'}`}>
                    {(result.min_separation_m || 2.0).toFixed(2)} m
                  </span>
                </div>
                <div className="diag-item">
                  <span className="diag-label">Site Radius:</span>
                  <span className={`diag-val ${(result.max_radius_m || 0) <= 30.0 ? 'text-success' : 'text-danger'}`}>
                    {(result.max_radius_m || 0).toFixed(1)} m / 30m
                  </span>
                </div>
                <div className="diag-item">
                  <span className="diag-label">Contours:</span>
                  <span className="diag-val">{result.num_contours || 0}</span>
                </div>
              </div>
            )}

            <div className="raster-footer-actions">
              <button
                type="button"
                className="btn-primary"
                onClick={handleInsert}
                disabled={!result?.points?.length || converting}
              >
                <Check size={14} /> Insert as Scene
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
