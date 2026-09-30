import { useState } from 'react';
import FleetHealthStrip from './FleetHealthStrip';
import StatusMatrix from './StatusMatrix';
import DroneCard from './DroneCard';

/**
 * Fleet state: the health strip, then one detail card per drone, or above
 * MATRIX_THRESHOLD drones a status matrix with a single drill-down card.
 */
export default function FleetTelemetryPanel({ telemetry, stats }) {
  const [selectedDroneId, setSelectedDroneId] = useState(null);
  const { droneIds, useMatrixView } = stats;

  return (
    <section className="gcs-panel">
      <h2 className="panel-header">FLEET TELEMETRY</h2>

      <FleetHealthStrip stats={stats} />

      {useMatrixView ? (
        <>
          <StatusMatrix
            droneIds={droneIds}
            telemetry={telemetry}
            selectedDroneId={selectedDroneId}
            onSelect={setSelectedDroneId}
          />
          <div className="fleet-grid">
            {selectedDroneId != null && telemetry[selectedDroneId]
              ? <DroneCard key={selectedDroneId} id={selectedDroneId} drone={telemetry[selectedDroneId]} />
              : (
                <div style={{ gridColumn: '1 / -1', padding: '2rem', textAlign: 'center', color: 'var(--text-muted)' }}>
                  Click a tile above to inspect that drone.
                </div>
              )}
          </div>
        </>
      ) : (
      <div className="fleet-grid">
        {droneIds.map((id) => <DroneCard key={id} id={id} drone={telemetry[id]} />)}

        {droneIds.length === 0 && (
          <div style={{ gridColumn: '1 / -1', padding: '3rem', textAlign: 'center', color: 'var(--text-muted)' }}>
            Waiting for telemetry stream from PX4 SITL simulator...
          </div>
        )}
      </div>
      )}
    </section>
  );
}
