import { PATTERNS, makeLedKeyframe } from '../../engine/ledPatterns.js';

/** Per-pattern controls. Kept beside the pattern list so adding one is a single edit. */
const FIELDS = {
  SOLID: [],
  COLOR_CYCLE: [],
  STROBE: [
    { key: 'frequency_hz', label: 'Rate (Hz)', min: 2, max: 8, step: 0.5 },
    { key: 'duty_cycle', label: 'Duty', min: 0.05, max: 0.95, step: 0.05 },
  ],
  SPARKLE: [{ key: 'density', label: 'Density', min: 0.01, max: 1, step: 0.01 }],
  WAVE_PROPAGATION: [
    { key: 'wave_speed', label: 'Speed (m/s)', min: 0.5, max: 20, step: 0.5 },
    { key: 'wave_length', label: 'Wavelength (m)', min: 1, max: 60, step: 1 },
  ],
};

/**
 * Multi-track LED choreography editor.
 *
 * Keyframes are laid out against the same time axis as the show timeline, so a
 * lighting cue lines up with the scene and the waveform above it. Editing is
 * form-based rather than drag-based: the acceptance criteria are about
 * sub-second alignment, and typing 12.25 is more precise than dragging to it.
 */
export default function LedTrackEditor({
  track, onChange, duration, currentTime, onScrub,
}) {
  const update = (id, patch) =>
    onChange(track.map((kf) => (kf.id === id ? { ...kf, ...patch } : kf)));

  const add = () => {
    // Drop the new cue at the playhead so it lands where the designer is
    // looking, clamped so it never starts past the end of the show.
    const start = Math.max(0, Math.min(currentTime, Math.max(0, duration - 1)));
    onChange([...track, makeLedKeyframe({ start_time: +start.toFixed(2), duration: 4 })]);
  };

  const remove = (id) => onChange(track.filter((kf) => kf.id !== id));

  const duplicate = (kf) => {
    const copy = makeLedKeyframe({ ...kf, start_time: +(kf.start_time + kf.duration).toFixed(2) });
    onChange([...track, copy]);
  };

  const pct = (t) => (duration > 0 ? Math.max(0, Math.min(100, (t / duration) * 100)) : 0);

  return (
    <div className="glass-panel-inner led-track-editor">
      <div className="led-track-head">
        <h4 className="section-subtitle">LED Choreography Track</h4>
        <button className="btn-ghost btn-inline" type="button" onClick={add} disabled={duration <= 0}>
          + Keyframe
        </button>
      </div>

      {/* Overview lane: every cue against the show's time axis. */}
      <div className="led-lane" role="presentation">
        {track.map((kf) => (
          <button
            key={kf.id}
            type="button"
            className={`led-lane-block led-${kf.pattern.toLowerCase()}`}
            style={{
              left: `${pct(kf.start_time)}%`,
              width: `${Math.max(1, pct(kf.start_time + kf.duration) - pct(kf.start_time))}%`,
              background: kf.pattern === 'COLOR_CYCLE'
                ? `linear-gradient(90deg, ${(kf.palette || []).join(',') || kf.color})`
                : kf.color,
            }}
            title={`${kf.pattern} · ${kf.start_time}s – ${(kf.start_time + kf.duration).toFixed(2)}s`}
            onClick={() => onScrub && onScrub(kf.start_time)}
          />
        ))}
        <div className="led-lane-playhead" style={{ left: `${pct(currentTime)}%` }} />
      </div>

      {track.length === 0 && (
        <p className="panel-hint">
          No lighting cues yet. Add one to colour the swarm; uncovered time
          stays dark.
        </p>
      )}

      {track.map((kf) => (
        <div key={kf.id} className="led-kf-card">
          <div className="led-kf-row">
            <select
              value={kf.pattern}
              onChange={(e) => update(kf.id, { pattern: e.target.value })}
              aria-label="Pattern"
            >
              {PATTERNS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
            </select>
            <input
              type="color"
              value={kf.color}
              onChange={(e) => update(kf.id, { color: e.target.value })}
              aria-label="Colour"
            />
            <button className="btn-ghost btn-tiny" type="button" onClick={() => duplicate(kf)} title="Duplicate">⧉</button>
            <button className="btn-ghost btn-tiny" type="button" onClick={() => remove(kf.id)} title="Delete">✕</button>
          </div>

          <div className="led-kf-row">
            <label className="ui-field-tight">
              <span className="ui-field-label">Start (s)</span>
              <input
                type="number" min={0} max={duration} step={0.05} value={kf.start_time}
                onChange={(e) => update(kf.id, { start_time: Math.max(0, Number(e.target.value)) })}
              />
            </label>
            <label className="ui-field-tight">
              <span className="ui-field-label">Length (s)</span>
              <input
                type="number" min={0.05} step={0.05} value={kf.duration}
                onChange={(e) => update(kf.id, { duration: Math.max(0.05, Number(e.target.value)) })}
              />
            </label>
          </div>

          {FIELDS[kf.pattern].length > 0 && (
            <div className="led-kf-row">
              {FIELDS[kf.pattern].map((f) => (
                <label key={f.key} className="ui-field-tight">
                  <span className="ui-field-label">{f.label}</span>
                  <input
                    type="number" min={f.min} max={f.max} step={f.step} value={kf[f.key]}
                    onChange={(e) => update(kf.id, { [f.key]: Number(e.target.value) })}
                  />
                </label>
              ))}
              {kf.pattern === 'WAVE_PROPAGATION' && (
                <label className="ui-field-tight">
                  <span className="ui-field-label">Axis</span>
                  <select value={kf.axis} onChange={(e) => update(kf.id, { axis: e.target.value })}>
                    <option value="x">X</option>
                    <option value="y">Y</option>
                    <option value="z">Z</option>
                  </select>
                </label>
              )}
            </div>
          )}

          {kf.pattern === 'COLOR_CYCLE' && (
            <div className="led-kf-row led-palette-row">
              <span className="ui-field-label">Palette</span>
              {(kf.palette || []).map((c, i) => (
                <input
                  // Palette entries have no stable identity of their own, and
                  // reordering isn't supported here, so index keys are safe.
                  key={i}
                  type="color"
                  value={c}
                  onChange={(e) => {
                    const next = [...kf.palette];
                    next[i] = e.target.value;
                    update(kf.id, { palette: next });
                  }}
                />
              ))}
              <button
                className="btn-ghost btn-tiny" type="button"
                onClick={() => update(kf.id, { palette: [...(kf.palette || []), '#ffffff'] })}
              >+</button>
              <button
                className="btn-ghost btn-tiny" type="button"
                disabled={(kf.palette || []).length <= 1}
                onClick={() => update(kf.id, { palette: kf.palette.slice(0, -1) })}
              >−</button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
