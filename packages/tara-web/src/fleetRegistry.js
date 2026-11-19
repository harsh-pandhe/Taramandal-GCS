/** The registry's drones as { [drone_id]: entry }, for lookups by tile and card. */
export const registryById = (registry) => {
  const map = {};
  for (const d of registry?.drones ?? []) map[d.drone_id] = d;
  return map;
};
