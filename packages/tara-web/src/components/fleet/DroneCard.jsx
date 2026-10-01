import { getBatteryColor } from '../../fleetStats';

/**
 * The full per-drone detail card. Used directly for small fleets (<= 12
 * drones, one grid of cards) and as the drill-down panel below the
 * status-matrix tile grid for larger ones.
 */
export default function DroneCard({ id, drone }) {
  if (!drone) return null;
  const isConn = drone.connected;
  return (
    <div className={`drone-card ${isConn ? '' : 'disconnected'}`}>
      <div className="drone-card-header">
        <div className="drone-id">
          DRONE {String(id).padStart(2, '0')}
          <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)', fontWeight: 400 }}>
            (Port {drone.port})
          </span>
        </div>
        <span className={`drone-badge ${(drone.flight_mode || 'unknown').toLowerCase()}`}>
          {drone.flight_mode}
        </span>
      </div>

      <div className="telemetry-subgrid">
        <div className="telemetry-item">
          <span className="telemetry-label">Status</span>
          <span className="telemetry-value" style={{ color: isConn ? 'var(--success)' : 'var(--danger)' }}>
            {isConn ? 'ONLINE' : 'OFFLINE'}
          </span>
        </div>
        <div className="telemetry-item">
          <span className="telemetry-label">Armed State</span>
          <span className="telemetry-value" style={{ color: drone.armed ? 'var(--warning)' : 'var(--text-muted)' }}>
            {drone.armed ? 'ARMED' : 'DISARMED'}
          </span>
        </div>
        <div className="telemetry-item">
          <span className="telemetry-label">GPS Satellites</span>
          <span className="telemetry-value">
            {isConn ? `${drone.satellites} SATS` : 'N/A'}
          </span>
        </div>
        <div className="telemetry-item">
          <span className="telemetry-label">GPS Lock</span>
          <span className="telemetry-value" style={{ color: drone.gps_lock ? 'var(--success)' : 'var(--danger)' }}>
            {isConn ? (drone.gps_lock ? 'FIX OK' : 'NO LOCK') : 'N/A'}
          </span>
        </div>
        <div className="telemetry-item">
          <span className="telemetry-label">RTK Status</span>
          <span className="telemetry-value" style={{
            color: isConn && (drone.gps_fix_type === 'RTK_FIXED' || drone.gps_fix_type === 'RTK_FLOAT') ? 'var(--primary)' : 'var(--text-muted)'
          }}>
            {isConn ? (drone.gps_fix_type || 'N/A') : 'N/A'}
          </span>
        </div>
        <div className="telemetry-item">
          <span className="telemetry-label">Latency</span>
          <span className="telemetry-value" style={{
            color: isConn ? (drone.comm_latency_ms < 50 ? 'var(--success)' : drone.comm_latency_ms < 150 ? 'var(--warning)' : 'var(--danger)') : 'var(--text-muted)'
          }}>
            {isConn ? `${drone.comm_latency_ms || 0.0} ms` : 'N/A'}
          </span>
        </div>
      </div>

      <div className="battery-container">
        <div className="battery-header">
          <span className="telemetry-label">Battery Level</span>
          <span className="telemetry-value" style={{ fontSize: '0.9rem' }}>
            {isConn ? `${drone.battery_percent}% (${drone.battery_voltage}V)` : 'N/A'}
          </span>
        </div>
        <div className="battery-bar-bg">
          <div
            className="battery-bar-fill"
            style={{
              width: isConn ? `${Math.min(drone.battery_percent, 100)}%` : '0%',
              backgroundColor: getBatteryColor(drone.battery_percent)
            }}
          />
        </div>
      </div>

      <div className="coords-container">
        <div className="coord-box">
          <span className="coord-label">Local X (North)</span>
          <span className="coord-val">{isConn ? `${drone.local_x}m` : '0.00m'}</span>
        </div>
        <div className="coord-box">
          <span className="coord-label">Local Y (East)</span>
          <span className="coord-val">{isConn ? `${drone.local_y}m` : '0.00m'}</span>
        </div>
        <div className="coord-box">
          <span className="coord-label">Altitude (Z)</span>
          <span className="coord-val" style={{ color: 'var(--success)' }}>
            {isConn ? `${drone.local_z}m` : '0.00m'}
          </span>
        </div>
      </div>
    </div>
  );
}
