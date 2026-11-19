import { droneTileStatus } from '../../fleetStats';

/** Compact per-drone tile grid for large fleets; clicking a tile selects it for drill-down. */
export default function StatusMatrix({ droneIds, telemetry, selectedDroneId, onSelect, registryById = {} }) {
  return (
    <div className="status-matrix">
      {droneIds.map((id) => {
        const drone = telemetry[id];
        const status = droneTileStatus(drone);
        const quiet = registryById[id]?.state === 'STALE';
        return (
          <button
            key={id}
            className={`status-tile status-tile-${status} ${selectedDroneId === id ? 'selected' : ''} ${quiet ? 'registry-stale' : ''}`}
            onClick={() => onSelect(id)}
            title={`Drone ${id}: ${status.replace(/-/g, ' ').toUpperCase()}${quiet ? ' · announcements STALE' : ''}`}
          >
            {String(id).padStart(2, '0')}
            {quiet && <span className="sr-only">announcements stale</span>}
          </button>
        );
      })}
    </div>
  );
}
