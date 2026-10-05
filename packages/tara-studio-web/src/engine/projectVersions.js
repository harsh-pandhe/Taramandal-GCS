// Project save history (issue #100).
//
// Saving used to overwrite the project's one stored snapshot. Each save now
// also pushes a version - a timestamped copy of the editable show - so an
// earlier save can be browsed and restored.
//
// A version holds what the designer edits (scenes, fleet, rate, home), not the
// built trajectory: a built 100-drone show is megabytes, twenty of them would
// blow the localStorage quota, and it can be regenerated from the scenes.

/** Versions kept per project; the oldest is dropped past this. */
export const MAX_VERSIONS = 20;

const FIELDS = ['scenes', 'rate', 'homeLat', 'homeLon', 'dronesList'];

const pick = (state) => Object.fromEntries(FIELDS.map((k) => [k, state[k]]));
const sameContent = (a, b) => JSON.stringify(pick(a)) === JSON.stringify(pick(b));

export const versionsOf = (project) => (Array.isArray(project?.versions) ? project.versions : []);

/**
 * Returns `project` with a new version of `state` at the front (newest first).
 * Saving twice without changing anything does not add a second identical
 * version; the same project object comes back.
 */
export const addVersion = (project, state, now = Date.now(), label = 'Saved') => {
  const versions = versionsOf(project);
  if (versions.length && sameContent(versions[0], state)) return project;
  const version = {
    id: `v-${now}-${versions.length}`,
    savedAt: new Date(now).toISOString(),
    label,
    ...pick(state),
  };
  return { ...project, versions: [version, ...versions].slice(0, MAX_VERSIONS) };
};

/** True when some version already holds exactly this state. */
export const hasVersionOf = (project, state) =>
  versionsOf(project).some((v) => sameContent(v, state));

export const findVersion = (project, versionId) =>
  versionsOf(project).find((v) => v.id === versionId) ?? null;

/**
 * Returns `project` put back to `version`. The built trajectory and its
 * validation are cleared: they belonged to the state being replaced.
 */
export const restoreVersion = (project, version) => ({
  ...project,
  ...pick(version),
  choreoData: null,
  validation: null,
});
