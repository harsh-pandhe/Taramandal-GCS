import { droneTileStatus } from '../../fleetStats';

/** Compact per-drone tile grid for large fleets; clicking a tile selects it for drill-down. */
export default function StatusMatrix({ droneIds, telemetry, selectedDroneId, onSelect }) {
  return (
    <div className="status-matrix">
      {droneIds.map((id) => {
        const drone = telemetry[id];
        const status = droneTileStatus(drone);
        return (
          <button
            key={id}
            className={`status-tile status-tile-${status} ${selectedDroneId === id ? 'selected' : ''}`}
            onClick={() => onSelect(id)}
            title={`Drone ${id}: ${status.toUpperCase()}`}
          >
            {String(id).padStart(2, '0')}
          </button>
        );
      })}
    </div>
  );
}
