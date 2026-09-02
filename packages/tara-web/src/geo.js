// Mirrors packages/tara-core/tara_core/geometry.py and layout.py's math by
// hand — kept in sync manually since there's no shared schema between the
// Python and JS sides. Used by EarthView to place launch pads and live
// drones at their real-world lat/lon.

export const EARTH_RADIUS_M = 6378137.0;

// The exact inverse of tara_core.geometry.ned_to_geodetic — same
// equirectangular approximation, accurate to well under a centimetre over
// the few hundred metres a drone show spans.
export function nedToGeodetic(x, y, homeLat, homeLon) {
  const lat = homeLat + (x / EARTH_RADIUS_M) * (180 / Math.PI);
  const lon = homeLon + (y / (EARTH_RADIUS_M * Math.cos((homeLat * Math.PI) / 180))) * (180 / Math.PI);
  return [lat, lon];
}

// Mirrors tara_core.layout.launch_grid: a near-square grid, row-major,
// anchored so drone 0 sits at the fleet-frame origin. Row width is capped at
// floor(geofenceRadius / spacing) so a single row can never itself reach past
// the geofence — see launch_grid's docstring for why (a fixed-size 1000-drone
// grid otherwise needs 32 columns, putting low ids like 16 outside the 30 m
// geofence at row 0, column 16, even in a 50-drone show). Below the cap
// (n <= 225 at the defaults) this is identical to the uncapped near-square
// grid, so ids under the row width still fall on a single x=0 line, matching
// sim/px4/launch_fleet.sh's real SITL spawn layout at the fleet sizes real
// SITL is practical for.
export function launchGrid(n, spacing = 2.0, geofenceRadius = 30.0) {
  if (n <= 0) return [];
  const maxCols = Math.max(1, Math.floor(geofenceRadius / spacing));
  const cols = Math.max(1, Math.min(Math.ceil(Math.sqrt(n)), maxCols));
  const pads = [];
  for (let i = 0; i < n; i++) {
    pads.push([Math.floor(i / cols) * spacing, (i % cols) * spacing]);
  }
  return pads;
}
