import { getBatteryColor } from '../../fleetStats';

/** Fleet-wide aggregates: connected, armed, GPS-locked, average and minimum battery. */
export default function FleetHealthStrip({ stats }) {
  const { droneIds, connectedCount, armedCount, gpsLockedCount, avgBattery, minBattery } = stats;
  return (
    <div className="fleet-health-strip">
      <div className="fleet-stat">
        <span className="fleet-stat-label">Connected</span>
        <span className="fleet-stat-value">{connectedCount}/{droneIds.length}</span>
      </div>
      <div className="fleet-stat">
        <span className="fleet-stat-label">Armed</span>
        <span className="fleet-stat-value" style={{ color: armedCount > 0 ? 'var(--warning)' : 'var(--text-muted)' }}>
          {armedCount}/{droneIds.length}
        </span>
      </div>
      <div className="fleet-stat">
        <span className="fleet-stat-label">GPS Locked</span>
        <span className="fleet-stat-value">{gpsLockedCount}/{droneIds.length}</span>
      </div>
      <div className="fleet-stat">
        <span className="fleet-stat-label">Avg Battery</span>
        <span className="fleet-stat-value" style={{ color: getBatteryColor(avgBattery) }}>
          {avgBattery.toFixed(0)}%
        </span>
      </div>
      <div className="fleet-stat">
        <span className="fleet-stat-label">Min Battery</span>
        <span className="fleet-stat-value" style={{ color: getBatteryColor(minBattery) }}>
          {droneIds.length ? `${minBattery.toFixed(0)}%` : 'N/A'}
        </span>
      </div>
    </div>
  );
}
