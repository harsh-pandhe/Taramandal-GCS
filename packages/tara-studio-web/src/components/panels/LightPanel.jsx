import { useState } from 'react';
import { LIGHT_EFFECTS, droneColorAt } from '../../engine/lighting.js';
import { AUTO_LIGHT_DIRECTIONS } from '../../engine/autoLight.js';

/** LED choreography settings plus a live swatch row at the playhead. */
export default function LightPanel({
  effect, onEffect, baseColor, onBaseColor,
  choreoData, currentTime, lightingOpts, ledTrack = [], onAutoLight, onClearTrack,
}) {
  const perDrone = effect === 'perDrone';
  const [direction, setDirection] = useState('ltr');
  const [sweepSeconds, setSweepSeconds] = useState(2.5);
  const [blinkCount, setBlinkCount] = useState(3);
  const [made, setMade] = useState(null);

  return (
    <div className="glass-panel-inner">
      <h4 className="section-subtitle">LED Choreography</h4>

      <h4 className="section-subtitle">Auto light show</h4>
      <p className="panel-hint">
        Lights go off while the drones fly. The moment a formation is reached they come on as a sweep across
        it, each drone blinking its colour as the front reaches it, then the colours keep running until the
        next move. Build the sequence first.
      </p>
      <label className="ui-field">
        <span className="ui-field-label">Sweep direction</span>
        <select value={direction} onChange={(e) => setDirection(e.target.value)} aria-label="Sweep direction">
          {AUTO_LIGHT_DIRECTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </label>
      <div className="led-kf-row">
        <label className="ui-field-tight">
          <span className="ui-field-label">Sweep time (s)</span>
          <input type="number" min={0.5} max={10} step={0.5} value={sweepSeconds} onChange={(e) => setSweepSeconds(Number(e.target.value))} />
        </label>
        <label className="ui-field-tight">
          <span className="ui-field-label">Blinks</span>
          <input type="number" min={0} max={8} step={1} value={blinkCount} onChange={(e) => setBlinkCount(Number(e.target.value))} />
        </label>
      </div>
      <div className="led-kf-row">
        <button
          type="button" className="btn-primary btn-inline" disabled={!choreoData || !onAutoLight}
          onClick={() => setMade(onAutoLight({ direction, sweepSeconds, blinkCount }))}
        >
          Create auto light show
        </button>
        <button type="button" className="btn-ghost btn-inline" disabled={!ledTrack.length || !onClearTrack} onClick={() => { onClearTrack(); setMade(0); }}>
          Clear lighting
        </button>
      </div>
      <p className="panel-hint">
        {ledTrack.length
          ? `${ledTrack.length} lighting cues are set (edit them under AUDIO). Uncovered time stays dark.`
          : (made === 0 ? 'Lighting cleared.' : 'No lighting cues yet.')}
      </p>

      <h4 className="section-subtitle">Static effect</h4>
      <label className="ui-field">
        <span className="ui-field-label">Light Effect</span>
        <select value={effect} onChange={(e) => onEffect(e.target.value)}>
          {LIGHT_EFFECTS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      </label>

      <label className="ui-field">
        <span className="ui-field-label">
          Base Colour {perDrone && '(uses Swarm Fleet colours)'}
        </span>
        <div className="color-row">
          <input type="color" value={baseColor} onChange={(e) => onBaseColor(e.target.value)} disabled={perDrone} />
          <input type="text" value={baseColor} onChange={(e) => onBaseColor(e.target.value)} disabled={perDrone} className="mono" />
        </div>
      </label>

      {choreoData && (
        <div>
          <span className="telemetry-label">Live @ {currentTime.toFixed(1)}s</span>
          <div className="swatch-row">
            {choreoData.drones.slice(0, 64).map((d, i) => {
              const hex = `#${droneColorAt(i, currentTime, lightingOpts).getHexString()}`;
              return <div key={i} className="swatch" title={`Drone ${String(i).padStart(2, '0')} · ${hex}`} style={{ background: hex }} />;
            })}
            {choreoData.drones.length > 64 && (
              <span className="swatch-more">+{choreoData.drones.length - 64}</span>
            )}
          </div>
        </div>
      )}

      <p className="panel-hint">
        Colour is live in the preview and follows the timeline. It bakes into
        CSV/JSON export as <span className="mono">r,g,b</span> per waypoint.
      </p>
    </div>
  );
}
