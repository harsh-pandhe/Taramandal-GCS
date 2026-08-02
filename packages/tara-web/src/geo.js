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
// anchored so drone 0 sits at the fleet-frame origin.
export function launchGrid(n, spacing = 2.0) {
  if (n <= 0) return [];
  const cols = Math.max(1, Math.ceil(Math.sqrt(n)));
  const pads = [];
  for (let i = 0; i < n; i++) {
    pads.push([Math.floor(i / cols) * spacing, (i % cols) * spacing]);
  }
  return pads;
}
