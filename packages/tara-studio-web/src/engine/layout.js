// Fleet ground layout: launch pads, the hover grid above them, and where
// formations are centred.
//
// Pads mirror tara_core.layout.launch_grid(MAX_FLEET_SIZE) exactly. The GCS
// converts every drone's fleet-frame trajectory against that one fixed grid
// (tara_gcs.drone_manager._pad_offset), so if the Studio puts drone i anywhere
// else, the show flies drone i from the wrong pad. A single 2 m row agreed with
// the grid only for the first 15 drones.

export const SHOW_ALT = 8.0; // metres above ground: lowest hover layer

// launch_grid spacing defaults to design_separation_m, and its row width is
// floor(geofence_radius_m / spacing) = floor(30 / 2).
export const PAD_SPACING = 2.0;
export const PAD_COLS = 15;

// Hovering drones sit on four staggered heights, checkerboarded over the pads,
// so any two are at least sqrt(2^2 + HOVER_STEP^2) = 3.2 m apart in 3D rather
// than the pads' bare 2.0 m. That spacing is what lets the sequencer fly
// straight-line morphs without altitude layering (see sequencer.js).
export const HOVER_STEP = 2.5;
export const HOVER_LEVELS = 4;

export const padAt = (i) => ({
  x: Math.floor(i / PAD_COLS) * PAD_SPACING,
  y: (i % PAD_COLS) * PAD_SPACING,
  z: 0,
});

// Row parity and column parity each pick a level, so row-, column- and
// diagonal-adjacent pads always hover at different heights.
const hoverLevel = (i) => (Math.floor(i / PAD_COLS) % 2) * 2 + ((i % PAD_COLS) % 2);

export const hoverAt = (i) => ({
  ...padAt(i),
  z: -(SHOW_ALT + HOVER_STEP * hoverLevel(i)),
});

/** Horizontal centre of the pad grid an n-drone fleet occupies. */
export const fleetCenter = (n) => {
  const rows = Math.max(1, Math.ceil(n / PAD_COLS));
  const cols = Math.max(1, Math.min(n, PAD_COLS));
  return { x: ((rows - 1) * PAD_SPACING) / 2, y: ((cols - 1) * PAD_SPACING) / 2 };
};
