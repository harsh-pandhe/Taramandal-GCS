import { LIGHT_EFFECTS, droneColorAt } from '../../engine/lighting.js';

/** LED choreography settings plus a live swatch row at the playhead. */
export default function LightPanel({
  effect, onEffect, baseColor, onBaseColor,
  choreoData, currentTime, lightingOpts,
}) {
  const perDrone = effect === 'perDrone';

  return (
    <div className="glass-panel-inner">
      <h4 className="section-subtitle">LED Choreography</h4>

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
