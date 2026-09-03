// Spatial trajectory deviation: how far each drone actually is from where the
// show says it should be, and the colour band that maps to.
//
// The thresholds are the show-director-facing spec from issue #23, not a
// rendering detail — they're kept here rather than inside the night-sky
// component so the viewport, any future HUD readout, and the tests all score
// deviation the same way instead of each carrying its own copy of the numbers.

export const NOMINAL_MAX_M = 0.4;   // e < 0.4  -> green
export const WARNING_MAX_M = 0.8;   // 0.4 <= e <= 0.8 -> yellow; e > 0.8 -> red

export const BAND_UNMONITORED = 'unmonitored';
export const BAND_NOMINAL = 'nominal';
export const BAND_WARNING = 'warning';
export const BAND_CRITICAL = 'critical';

// Colours are authored at >1.0 on the channel that carries the signal so the
// UnrealBloom pass has something above the luminance threshold to bleed —
// a plain #34d399 reads as a dim dot once bloom is doing the work.
export const BAND_COLORS = {
  [BAND_UNMONITORED]: [0.34, 0.38, 0.47],
  [BAND_NOMINAL]: [0.10, 1.60, 0.75],
  [BAND_WARNING]: [1.80, 1.35, 0.10],
  [BAND_CRITICAL]: [2.20, 0.16, 0.20],
};

// Euclidean |p_actual - p_planned| in metres, in the shared fleet frame.
// Returns NaN when the drone has no live plan to be scored against — callers
// must treat that as "unmonitored", distinct from a genuine zero deviation.
export function deviationMetres(drone) {
  if (!drone || !drone.planned_valid) return NaN;
  const dx = (drone.fleet_x || 0) - (drone.planned_x || 0);
  const dy = (drone.fleet_y || 0) - (drone.planned_y || 0);
  const dz = (drone.fleet_z || 0) - (drone.planned_z || 0);
  return Math.sqrt(dx * dx + dy * dy + dz * dz);
}

// Band boundaries follow the issue's spec literally: the 0.4 and 0.8 marks
// themselves are warning, not nominal/critical respectively (e < 0.4 nominal,
// 0.4 <= e <= 0.8 warning, e > 0.8 critical).
export function deviationBand(error) {
  if (!Number.isFinite(error)) return BAND_UNMONITORED;
  if (error < NOMINAL_MAX_M) return BAND_NOMINAL;
  if (error <= WARNING_MAX_M) return BAND_WARNING;
  return BAND_CRITICAL;
}

// Fleet-wide counts per band plus the worst offender, for the HUD summary.
// Single pass — this runs every frame at up to 1000 drones.
export function summarizeDeviation(drones) {
  const summary = {
    counts: {
      [BAND_UNMONITORED]: 0,
      [BAND_NOMINAL]: 0,
      [BAND_WARNING]: 0,
      [BAND_CRITICAL]: 0,
    },
    monitored: 0,
    worstError: 0,
    worstId: null,
  };
  for (const drone of drones) {
    const error = deviationMetres(drone);
    const band = deviationBand(error);
    summary.counts[band] += 1;
    if (band === BAND_UNMONITORED) continue;
    summary.monitored += 1;
    if (error > summary.worstError) {
      summary.worstError = error;
      summary.worstId = drone.id ?? null;
    }
  }
  return summary;
}
