import { interpolateWaypoint } from '../../engine/sequencer.js';

/** Live per-vehicle telemetry readout at the current playhead. */
export default function MatrixView({ choreoData, currentTime, dronesList, rate }) {
  if (!choreoData) return null;

  return (
    <div className="matrix-view-container">
      <h3 className="matrix-title">TELEMETRY MATRIX</h3>
      <div className="matrix-grid">
        {choreoData.drones.map((drone, idx) => {
          const wp = interpolateWaypoint(drone.waypoints, currentTime);
          // Finite-difference the speed over one sample interval, clamped to the
          // start of the show so t-dt never reads before the first waypoint.
          const dt = 1 / Math.max(1, rate);
          const prev = interpolateWaypoint(drone.waypoints, Math.max(0, currentTime - dt));
          const speed = wp && prev && currentTime > 0
            ? Math.hypot(wp.x - prev.x, wp.y - prev.y, wp.z - prev.z) / dt
            : 0;

          const cfg = dronesList.find((d) => d.id === idx);
          const color = cfg?.colorHex ?? '#38bdf8';
          const name = cfg?.name ?? `Vehicle ${String(idx).padStart(2, '0')}`;

          return (
            <div className="matrix-card" key={idx} style={{ borderLeft: `4px solid ${color}` }}>
              <div className="matrix-card-header">
                <span className="matrix-drone-id">{name.toUpperCase()}</span>
                <span className="matrix-status" style={{ color }}>STREAMING</span>
              </div>
              <div className="matrix-data-row">
                <div className="matrix-data-item"><label>LOCAL X</label><span className="value">{(wp?.x ?? 0).toFixed(2)}m</span></div>
                <div className="matrix-data-item"><label>LOCAL Y</label><span className="value">{(wp?.y ?? 0).toFixed(2)}m</span></div>
                <div className="matrix-data-item"><label>ALT (AGL)</label><span className="value">{(-(wp?.z ?? 0)).toFixed(2)}m</span></div>
                <div className="matrix-data-item"><label>YAW</label><span className="value">{(wp?.yaw ?? 0).toFixed(1)}°</span></div>
                <div className="matrix-data-item">
                  <label>VELOCITY</label>
                  <span className="value" style={{ color: speed > 4.0 ? 'var(--danger)' : '#10b981' }}>{speed.toFixed(2)} m/s</span>
                </div>
                <div className="matrix-data-item"><label>SAMPLES</label><span className="value">{drone.waypoints.length}</span></div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
