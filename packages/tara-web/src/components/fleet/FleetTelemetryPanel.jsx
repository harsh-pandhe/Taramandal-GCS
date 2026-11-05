import { useState, useMemo } from 'react';
import FleetHealthStrip from './FleetHealthStrip';
import StatusMatrix from './StatusMatrix';
import DroneCard from './DroneCard';
import { droneTileStatus } from '../../fleetStats';

/**
 * Fleet Cockpit (Issue #93): health strip, filtering, sorting, matrix/grid views,
 * and drill-down cards with rapid identification of degraded drones.
 */
export default function FleetTelemetryPanel({ telemetry, stats }) {
  const [selectedDroneId, setSelectedDroneId] = useState(null);
  const [filter, setFilter] = useState('all'); // 'all' | 'ready' | 'armed' | 'attention' | 'offline'
  const [search, setSearch] = useState('');
  const [sortBy, setSortBy] = useState('id-asc');
  const [viewOverride, setViewOverride] = useState(null); // null (auto) | 'matrix' | 'grid'

  const { droneIds, useMatrixView: autoMatrix } = stats;
  const isMatrixView = viewOverride !== null ? viewOverride === 'matrix' : autoMatrix;

  // Categorize drones for rapid filter counts
  const counts = useMemo(() => {
    let ready = 0;
    let armed = 0;
    let attention = 0;
    let offline = 0;
    droneIds.forEach((id) => {
      const drone = telemetry[id];
      const status = droneTileStatus(drone);
      if (status === 'ready') ready += 1;
      else if (status === 'armed') armed += 1;
      else if (status === 'offline') offline += 1;

      if (
        status === 'critical' ||
        status === 'armed-no-link' ||
        (drone?.connected && (drone.battery_percent < 25 || (drone.comm_latency_ms || 0) > 150))
      ) {
        attention += 1;
      }
    });
    return { ready, armed, attention, offline };
  }, [droneIds, telemetry]);

  // Filtered and sorted drone IDs
  const filteredIds = useMemo(() => {
    let list = droneIds.filter((id) => {
      const drone = telemetry[id];
      const status = droneTileStatus(drone);

      if (filter === 'ready' && status !== 'ready') return false;
      if (filter === 'armed' && status !== 'armed') return false;
      if (filter === 'offline' && status !== 'offline') return false;
      if (filter === 'attention') {
        const isDegraded =
          status === 'critical' ||
          status === 'armed-no-link' ||
          (drone?.connected && (drone.battery_percent < 25 || (drone.comm_latency_ms || 0) > 150));
        if (!isDegraded) return false;
      }

      if (search.trim()) {
        const query = search.trim().toLowerCase();
        if (!String(id).toLowerCase().includes(query)) return false;
      }
      return true;
    });

    list.sort((a, b) => {
      const da = telemetry[a] || {};
      const db = telemetry[b] || {};
      if (sortBy === 'battery-asc') {
        return (da.battery_percent ?? 100) - (db.battery_percent ?? 100);
      }
      if (sortBy === 'latency-desc') {
        return (db.comm_latency_ms ?? 0) - (da.comm_latency_ms ?? 0);
      }
      if (sortBy === 'alt-desc') {
        return (db.local_z ?? 0) - (da.local_z ?? 0);
      }
      // default: id numeric ascending
      const numA = parseInt(a, 10);
      const numB = parseInt(b, 10);
      if (!Number.isNaN(numA) && !Number.isNaN(numB)) return numA - numB;
      return String(a).localeCompare(String(b));
    });

    return list;
  }, [droneIds, telemetry, filter, search, sortBy]);

  // Quick focus on first attention-requiring drone
  const handleFocusAttention = () => {
    const target = droneIds.find((id) => {
      const d = telemetry[id];
      const s = droneTileStatus(d);
      return s === 'critical' || s === 'armed-no-link' || (d?.connected && d.battery_percent < 25);
    });
    if (target != null) {
      setSelectedDroneId(target);
      setFilter('attention');
    }
  };

  return (
    <section className="gcs-panel">
      <h2 className="panel-header">FLEET TELEMETRY</h2>

      <FleetHealthStrip stats={stats} />

      {/* Cockpit Toolbar: filters, search, sort, and view switcher */}
      {droneIds.length > 0 && (
        <div className="fleet-toolbar">
          <div className="fleet-filter-group">
            <button
              type="button"
              className={`fleet-filter-chip ${filter === 'all' ? 'active' : ''}`}
              onClick={() => setFilter('all')}
            >
              All ({droneIds.length})
            </button>
            <button
              type="button"
              className={`fleet-filter-chip ${filter === 'ready' ? 'active' : ''}`}
              onClick={() => setFilter('ready')}
            >
              Ready ({counts.ready})
            </button>
            <button
              type="button"
              className={`fleet-filter-chip ${filter === 'armed' ? 'active' : ''}`}
              onClick={() => setFilter('armed')}
            >
              Armed ({counts.armed})
            </button>
            {counts.attention > 0 && (
              <button
                type="button"
                className={`fleet-filter-chip chip-danger ${filter === 'attention' ? 'active' : ''}`}
                onClick={() => setFilter('attention')}
              >
                ⚠ Attention ({counts.attention})
              </button>
            )}
            <button
              type="button"
              className={`fleet-filter-chip ${filter === 'offline' ? 'active' : ''}`}
              onClick={() => setFilter('offline')}
            >
              Offline ({counts.offline})
            </button>
          </div>

          <div className="fleet-search-box">
            {counts.attention > 0 && filter !== 'attention' && (
              <button
                type="button"
                className="btn-tiny btn-ghost"
                style={{ color: 'var(--danger)', fontSize: '0.72rem' }}
                onClick={handleFocusAttention}
                title="Immediately select first drone requiring attention"
              >
                Inspect Issue
              </button>
            )}

            <input
              type="text"
              placeholder="Search ID…"
              className="fleet-search-input"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />

            <select
              className="fleet-select"
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              title="Sort fleet list"
            >
              <option value="id-asc">ID (0–9)</option>
              <option value="battery-asc">Lowest Battery</option>
              <option value="latency-desc">Highest Latency</option>
              <option value="alt-desc">Highest Altitude</option>
            </select>

            <button
              type="button"
              className="fleet-filter-chip"
              onClick={() => setViewOverride(isMatrixView ? 'grid' : 'matrix')}
              title={`Switch to ${isMatrixView ? 'Full Grid' : 'Compact Matrix'}`}
            >
              {isMatrixView ? '🗂 Grid' : '▦ Matrix'}
            </button>
          </div>
        </div>
      )}

      {isMatrixView ? (
        <>
          <StatusMatrix
            droneIds={filteredIds}
            telemetry={telemetry}
            selectedDroneId={selectedDroneId}
            onSelect={setSelectedDroneId}
          />
          <div className="fleet-grid">
            {selectedDroneId != null && telemetry[selectedDroneId]
              ? <DroneCard key={selectedDroneId} id={selectedDroneId} drone={telemetry[selectedDroneId]} />
              : (
                <div style={{ gridColumn: '1 / -1', padding: '2rem', textAlign: 'center', color: 'var(--text-muted)' }}>
                  {filteredIds.length ? 'Click a tile above to inspect that drone.' : 'No drones match the current filter.'}
                </div>
              )}
          </div>
        </>
      ) : (
        <div className="fleet-grid">
          {filteredIds.map((id) => <DroneCard key={id} id={id} drone={telemetry[id]} />)}

          {filteredIds.length === 0 && droneIds.length > 0 && (
            <div style={{ gridColumn: '1 / -1', padding: '2rem', textAlign: 'center', color: 'var(--text-muted)' }}>
              No drones match the current filter.
            </div>
          )}

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
