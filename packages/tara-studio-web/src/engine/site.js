// Site profiles: the horizontal site a show is built for (issue: per-show site).
//
// The radius is a property of the *show*, not a global default. "standard" is the 30 m site with 13 pad
// columns every demo before the 400 drone show was built on; "scale400" is 60 m with 27. The height ceiling
// is the same in every profile (120 m, safetyLimits.js), and so is every spacing limit.
//
// This table mirrors tara_core.safety.SITE_RADII_M, and the pad columns are the same function of the radius
// as tara_core.layout.launch_grid's: floor(radius / pad spacing). site.test.js pins both, so the Studio's pads
// cannot drift from the GCS's (the GCS converts every drone's trajectory against its own grid for the show's
// site, so a Studio that put drone i on a different pad would fly drone i from the wrong one).

export const STANDARD_SITE = 'standard';

const PAD_SPACING_M = 2.2; // layout.js PAD_SPACING, tara_core pad_spacing_m

export const SITE_RADII_M = { standard: 30.0, scale400: 60.0 };

export const SITE_PROFILES = Object.fromEntries(
  Object.entries(SITE_RADII_M).map(([name, radius]) => [name, { name, radius_m: radius, padCols: Math.floor(radius / PAD_SPACING_M) }]),
);

export const SITE_NAMES = Object.keys(SITE_PROFILES);

/** The profile for a name; absent means standard. An unknown name throws: a show must never be built on a guessed site. */
export const siteProfile = (name) => {
  const key = name == null ? STANDARD_SITE : name;
  const profile = SITE_PROFILES[key];
  if (!profile) throw new Error(`unknown site profile "${key}": expected one of ${SITE_NAMES.join(', ')}`);
  return profile;
};
