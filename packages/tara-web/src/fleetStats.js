// Fleet-wide figures derived from the telemetry map, shared by the fleet
// panel, the pre-flight checklist and the launch gate.

// Above this many drones, per-drone cards (340px+ each) stop being a usable
// layout — a compact status-matrix tile grid plus a single drill-down
// detail panel replaces it instead.
export const MATRIX_THRESHOLD = 12;

export function computeFleetStats(telemetry) {
  const droneIds = Object.keys(telemetry);
  const connectedCount = droneIds.filter(id => telemetry[id].connected).length;
  const totalDrones = droneIds.length || 3;

  const allConnected = droneIds.length > 0 && connectedCount === droneIds.length;

  // Fleet-wide aggregates for the status strip, and the matrix/card layout switch.
  const useMatrixView = droneIds.length > MATRIX_THRESHOLD;
  const armedCount = droneIds.filter(id => telemetry[id].armed).length;
  const gpsLockedCount = droneIds.filter(id => telemetry[id].gps_lock).length;
  const avgBattery = droneIds.length
    ? droneIds.reduce((sum, id) => sum + (telemetry[id].battery_percent || 0), 0) / droneIds.length
    : 0;
  const minBattery = droneIds.length
    ? Math.min(...droneIds.map(id => telemetry[id].battery_percent ?? 100))
    : 0;

  const allGpsLocked = droneIds.length > 0 && droneIds.every(id =>
    !telemetry[id].connected || telemetry[id].gps_lock
  );

  const allBatterySafe = droneIds.length > 0 && droneIds.every(id =>
    !telemetry[id].connected || telemetry[id].battery_percent > 75
  );

  const checklistPassed = allConnected && allGpsLocked && allBatterySafe;

  return {
    droneIds, connectedCount, totalDrones, allConnected, useMatrixView,
    armedCount, gpsLockedCount, avgBattery, minBattery,
    allGpsLocked, allBatterySafe, checklistPassed,
  };
}

export function droneTileStatus(drone) {
  if (!drone || !drone.connected) return 'offline';
  if (drone.battery_percent != null && drone.battery_percent < 20) return 'critical';
  if (drone.armed) return 'armed';
  return 'ready';
}

// Battery bar color mapping
export const getBatteryColor = (percent) => {
  if (percent > 50) return '#34d399';
  if (percent > 25) return '#fbbf24';
  return '#f87171';
};
