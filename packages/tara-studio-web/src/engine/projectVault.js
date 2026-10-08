// The Project Vault (issue #101): projects kept on the Studio backend.
//
// Projects used to live in this browser's localStorage and nowhere else. They
// are now read and written through /api/projects, so a show saved here opens
// in another browser and survives a cleared cache.
//
// Three layers, none of which touches React, so all of it is tested directly:
//
//   toWire / fromWire      the project as the editor holds it <-> as the
//                          backend stores it
//   createVaultClient      one function per endpoint
//   createProjectVault     what the Studio does with them: open the vault,
//                          save, load, create, delete, restore a saved version
//
// Save history is still computed by projectVersions.js (issue #100) and stored
// as part of the project; nothing about its rules changed.
//
// Single writer: the backend refuses a save that is not based on the project's
// latest revision. When that happens the edits are saved as a separate project
// instead - neither copy is overwritten and nothing is lost.

import { API_BASE } from './api.js';
import { makeScene } from './sequencer.js';
import { defaultFleet } from './fleetCommands.js';
import {
  MAX_VERSIONS, addVersion, findVersion, hasVersionOf, restoreVersion, sameContent, versionsOf,
} from './projectVersions.js';

/** Where projects were kept before the vault. Read once, to import; never deleted. */
export const LEGACY_KEY = 'taramandal_projects';
/** Set once this browser's old projects are all in the vault. */
export const MIGRATED_KEY = 'taramandal_projects_migrated';
/** The project this browser had open last, so a reload reopens it. */
export const ACTIVE_KEY = 'taramandal_active_project';

export const DEFAULT_PROJECT_ID = 'default-proj';
export const DEFAULT_PROJECT_NAME = 'Untitled Show';
const MAX_NAME_LENGTH = 200;

const DEFAULT_HOME = { homeLat: 12.9716, homeLon: 77.5946 };

/** An empty show, as "New Show" creates it. */
export const blankProject = (name = 'New Show') => ({
  name,
  scenes: [makeScene({ name: 'Scene 1' })],
  rate: 4,
  ...DEFAULT_HOME,
  dronesList: defaultFleet(6),
  choreoData: null,
  validation: null,
});

// ------------------------------------------------------------- wire format

const validVersion = (v) => v && typeof v.id === 'string' && v.id
  && typeof v.savedAt === 'string' && v.savedAt;

/** The request body for a project: the backend's shape, from the editor's. */
export const toWire = (project) => ({
  name: (String(project.name ?? '').trim() || DEFAULT_PROJECT_NAME).slice(0, MAX_NAME_LENGTH),
  document: {
    scenes: Array.isArray(project.scenes) ? project.scenes : [],
    dronesList: Array.isArray(project.dronesList) ? project.dronesList : [],
    rate: project.rate ?? 4,
    homeLat: project.homeLat ?? DEFAULT_HOME.homeLat,
    homeLon: project.homeLon ?? DEFAULT_HOME.homeLon,
  },
  // The built trajectory is a show_v1 document (issue #55) and is stored as one.
  show: project.choreoData ?? null,
  validation: project.validation ?? null,
  versions: versionsOf(project).filter(validVersion).slice(0, MAX_VERSIONS),
});

/**
 * A project as the editor and the vault page hold it. A listed project has no
 * `choreoData` (the list leaves the trajectory out); `hasShow` says whether
 * the vault holds one.
 */
export const fromWire = (p) => ({
  ...p.document,
  id: p.id,
  name: p.name,
  revision: p.revision,
  createdAt: p.created_at,
  updatedAt: p.updated_at,
  validation: p.validation ?? null,
  versions: Array.isArray(p.versions) ? p.versions : [],
  hasShow: !!p.has_show,
  choreoData: p.show ?? null,
});

// ------------------------------------------------------------------ client

export class VaultError extends Error {
  constructor(message, { status = 0, code = 'unreachable', detail = null } = {}) {
    super(message);
    this.name = 'VaultError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }

  /** The backend could not be reached, or failed in a way a retry may fix. */
  get offline() { return this.status === 0 || this.status >= 500; }
}

/**
 * One function per vault endpoint. `create`, `save` and `importLegacy`
 * resolve to the backend's `{ project, warnings }`; every failure rejects
 * with a VaultError carrying the backend's error code.
 */
export const createVaultClient = ({ base = API_BASE, fetchImpl } = {}) => {
  const call = async (method, path, body) => {
    let res;
    try {
      res = await (fetchImpl ?? globalThis.fetch)(`${base}/api/projects${path}`, {
        method,
        ...(body === undefined ? {} : {
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        }),
      });
    } catch {
      throw new VaultError('The Project Vault cannot be reached. Is the Studio backend running?');
    }
    if (res.status === 204) return null;
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = data?.detail;
      throw new VaultError(
        typeof detail?.message === 'string' ? detail.message : `The Project Vault request failed (${res.status}).`,
        { status: res.status, code: detail?.code ?? 'http', detail },
      );
    }
    return data;
  };
  const at = (id) => `/${encodeURIComponent(id)}`;

  return {
    list: async () => (await call('GET', '')).projects.map(fromWire),
    get: async (id) => fromWire((await call('GET', at(id))).project),
    create: (project, id) => call('POST', '', { ...toWire(project), ...(id ? { id } : {}) }),
    save: (id, project, baseRevision) => call('PUT', at(id), { ...toWire(project), base_revision: baseRevision }),
    remove: (id) => call('DELETE', at(id)),
    importLegacy: (project) => call('POST', '/import', {
      ...toWire(project), ...(typeof project.id === 'string' ? { id: project.id } : {}),
    }),
  };
};

// ---------------------------------------------------------- browser storage

// localStorage can be missing or throw (private windows, blocked site data).
// The vault is the backend; nothing here is allowed to break the Studio.
const read = (storage, key) => {
  try { return storage?.getItem(key) ?? null; } catch { return null; }
};
const write = (storage, key, value) => {
  try { storage?.setItem(key, value); } catch { /* a convenience, not the vault */ }
};

/** The projects this browser saved before the vault existed. */
export const readLegacyProjects = (storage) => {
  try {
    const parsed = JSON.parse(read(storage, LEGACY_KEY) ?? 'null');
    return Array.isArray(parsed) ? parsed.filter((p) => p && typeof p === 'object') : [];
  } catch {
    return [];
  }
};

/**
 * Copies this browser's old localStorage projects into the vault, once.
 *
 * The backend makes a repeat harmless (it recognises content it already
 * imported), and the old entry is left where it is as a backup. A project the
 * backend refuses is reported and tried again next time; a backend that is
 * down rejects, so nothing is marked as migrated.
 */
export const migrateLegacyProjects = async (client, storage, now = Date.now) => {
  const result = { imported: 0, failed: [] };
  if (read(storage, MIGRATED_KEY)) return result;
  const legacy = readLegacyProjects(storage);
  if (!legacy.length) return result;

  for (const project of legacy) {
    try {
      const { status } = await client.importLegacy(project);
      if (status !== 'skipped') result.imported += 1;
    } catch (e) {
      if (e.offline) throw e;
      result.failed.push({ name: project.name ?? project.id ?? 'unnamed', message: e.message });
    }
  }
  if (!result.failed.length) write(storage, MIGRATED_KEY, new Date(now()).toISOString());
  return result;
};

// -------------------------------------------------------------- controller

// Worded here, by error code, where the next step for the designer is known.
const MESSAGES = {
  revision_conflict: 'This project was saved somewhere else since it was opened here. '
    + 'Open it again from the Project Vault to continue from the newer copy.',
  project_not_found: 'That project is no longer in the Project Vault.',
};
const describe = (e) => {
  if (!(e instanceof VaultError)) return `Project Vault error: ${e?.message ?? e}`;
  return MESSAGES[e.code] ?? e.message;
};

const migrationNote = (failed) => (failed.length
  ? `${failed.length} project(s) saved in this browser could not be moved to the Project Vault `
    + `(${failed.map((f) => `${f.name}: ${f.message}`).join('; ')}). They are still in this browser's storage.`
  : null);

const showWarning = (warnings) => warnings?.find((w) => w.code === 'show_schema')?.message ?? null;

/**
 * The Studio's projects, backed by the vault.
 *
 * Holds the project list and which one is open; calls `onChange(state)` when
 * either changes. State is `{ status, projects, activeId, error }`, with
 * status 'loading', 'ready' or 'offline'.
 *
 * Every operation takes what it needs of the editor as an argument (`editor`:
 * name, scenes, rate, home, fleet, built show, validation) and resolves to
 * `{ editor, name }`: `editor` is a project to put in the editor, or null to
 * leave the editor alone; `name` is the open project's name when only that
 * changed. Nothing rejects - a failure lands in `state.error`.
 *
 * Operations run one at a time, in the order they were called: renaming a
 * project and clicking Save fires two saves back to back, and the second must
 * be based on the first's revision.
 */
export function createProjectVault({ client, storage = null, now = Date.now, onChange = () => {} }) {
  let state = { status: 'loading', projects: [], activeId: null, error: null };
  // The revision the editor's content is based on. Kept apart from the list,
  // which a refresh may move ahead: a save must still conflict if someone
  // else saved in between.
  let base = null;
  let queue = Promise.resolve();

  const set = (patch) => { state = { ...state, ...patch }; onChange(state); };
  const serial = (op) => {
    const run = queue.then(op);
    queue = run.catch(() => {});
    return run;
  };
  const NOTHING = { editor: null, name: null };
  const guarded = (op) => serial(async () => {
    try {
      return (await op()) ?? NOTHING;
    } catch (e) {
      // A vault that never finished opening is not usable either way.
      const offline = e.offline || state.status === 'loading';
      set({ error: describe(e), ...(offline ? { status: 'offline' } : {}) });
      return NOTHING;
    }
  });

  const find = (id) => state.projects.find((p) => p.id === id) ?? null;
  const put = (project) => (find(project.id)
    ? state.projects.map((p) => (p.id === project.id ? project : p))
    : [...state.projects, project]);
  const activate = (project, patch = {}) => {
    base = { id: project.id, revision: project.revision };
    write(storage, ACTIVE_KEY, project.id);
    set({ status: 'ready', projects: put(project), activeId: project.id, ...patch });
  };
  // What the backend stored, as a list entry. The response leaves the
  // trajectory out, so it is taken from what was sent - unless the backend
  // turned it down. The validation report is kept by reference too, so the
  // next save can tell that neither changed.
  const stored = (res, sent) => ({
    ...fromWire(res.project),
    choreoData: res.project.has_show ? (sent.choreoData ?? null) : null,
    validation: sent.validation ?? null,
  });

  const connect = async () => {
    let projects = await client.list();
    const migration = await migrateLegacyProjects(client, storage, now);
    if (migration.imported) projects = await client.list();
    return { projects, note: migrationNote(migration.failed) };
  };

  // Stores `editor` as a new project and makes it the open one.
  const createFrom = async (editor, name, id, note = null) => {
    const draft = addVersion({ ...editor, name }, editor, now());
    const res = await client.create(draft, id);
    const project = stored(res, editor);
    activate(project, { error: note ?? showWarning(res.warnings) });
    return project;
  };

  /**
   * Connects to the vault and decides what the editor shows.
   *
   * A vault with projects: the one this browser had open last (else the
   * first) is fetched and returned for the editor. An empty vault: the
   * editor's current show becomes its first project, so what is on screen and
   * what Save writes to are the same thing from the start.
   */
  const open = (editor) => guarded(async () => {
    set({ status: 'loading', error: null });
    let { projects, note } = await connect();

    if (!projects.length) {
      try {
        const res = await client.create({ ...editor, name: DEFAULT_PROJECT_NAME }, DEFAULT_PROJECT_ID);
        activate(stored(res, editor), { error: note });
        return NOTHING;
      } catch (e) {
        // Another browser opened the empty vault at the same moment.
        if (e.code !== 'project_exists') throw e;
        projects = await client.list();
      }
    }

    const wanted = read(storage, ACTIVE_KEY);
    const target = projects.find((p) => p.id === wanted) ?? projects[0];
    const project = await client.get(target.id);
    state = { ...state, projects };
    activate(project, { error: note });
    return { editor: project, name: project.name };
  });

  /**
   * Connects again after the vault was unreachable, without touching the
   * editor: whatever was built while offline stays on screen, and the next
   * Save stores it as a new project.
   */
  const retry = (editor) => guarded(async () => {
    set({ error: null });
    const { projects, note } = await connect();
    if (!projects.length) {
      await createFrom(editor, editor.name || DEFAULT_PROJECT_NAME, DEFAULT_PROJECT_ID, note);
      return NOTHING;
    }
    base = null;
    set({ status: 'ready', projects, activeId: null, error: note });
    return NOTHING;
  });

  /** Reads the project list again, e.g. to see what another browser saved. */
  const refresh = () => guarded(async () => {
    if (state.status !== 'ready') return NOTHING;
    const listed = await client.list();
    const open_ = find(state.activeId);
    // The list has no trajectories; keep the open project's entry as it is,
    // since it describes what this editor last loaded or saved.
    set({ projects: listed.map((p) => (open_ && p.id === open_.id ? open_ : p)) });
    return NOTHING;
  });

  const save = (editor) => guarded(async () => {
    if (state.status !== 'ready') {
      const { projects, note } = await connect();
      base = null;
      set({ status: 'ready', projects, activeId: null, error: note });
    }
    const current = find(state.activeId);
    const name = editor.name || DEFAULT_PROJECT_NAME;
    if (!current || !base || base.id !== current.id) {
      await createFrom(editor, name);
      return NOTHING;
    }

    const next = addVersion({ ...current, ...editor }, editor, now());
    const unchanged = versionsOf(next) === versionsOf(current)
      && current.name === next.name
      && current.choreoData === next.choreoData
      && current.validation === next.validation
      && sameContent(current, next);
    if (unchanged) return NOTHING;

    try {
      const res = await client.save(current.id, next, base.revision);
      activate(stored(res, editor), { error: showWarning(res.warnings) });
      return NOTHING;
    } catch (e) {
      // Saved elsewhere since it was opened here, or deleted elsewhere. The
      // edits go into a project of their own rather than over the other copy.
      if (e.code === 'revision_conflict') {
        const copy = await createFrom(
          { ...editor, versions: versionsOf(next) }, `${name} (conflicted copy)`.slice(0, MAX_NAME_LENGTH), undefined,
          `"${name}" was saved somewhere else since it was opened here, so your edits were saved as `
          + `a separate project, "${name} (conflicted copy)". Neither copy was overwritten.`,
        );
        return { editor: null, name: copy.name };
      }
      if (e.code === 'project_not_found') {
        set({ projects: state.projects.filter((p) => p.id !== current.id) });
        await createFrom(
          { ...editor, versions: versionsOf(next) }, name, undefined,
          `"${name}" was deleted from the Project Vault somewhere else; it was saved again as a new project.`,
        );
        return NOTHING;
      }
      throw e;
    }
  });

  const load = (id) => guarded(async () => {
    try {
      const project = await client.get(id);
      activate(project, { error: null });
      return { editor: project, name: project.name };
    } catch (e) {
      if (e.code === 'project_not_found') set({ projects: state.projects.filter((p) => p.id !== id) });
      throw e;
    }
  });

  const create = () => guarded(async () => {
    const blank = blankProject();
    const res = await client.create(blank);
    const project = stored(res, blank);
    activate(project, { error: null });
    return { editor: project, name: project.name };
  });

  const remove = (id) => guarded(async () => {
    if (state.projects.length <= 1 || !find(id)) return NOTHING;
    try {
      await client.remove(id);
    } catch (e) {
      if (e.code !== 'project_not_found') throw e;   // already gone: the same outcome
    }
    const remaining = state.projects.filter((p) => p.id !== id);
    if (id !== state.activeId) {
      set({ projects: remaining, error: null });
      return NOTHING;
    }
    // Deleting the open project has to move the editor somewhere valid.
    set({ projects: remaining, error: null });
    const project = await client.get(remaining[0].id);
    activate(project);
    return { editor: project, name: project.name };
  });

  /**
   * Puts a project back to one of its saved versions and opens it. If the
   * state being replaced is not already in the history (unsaved edits), it is
   * kept as a "Before restore" version first, so a restore never loses work.
   */
  const restore = (id, versionId, editor) => guarded(async () => {
    const listed = find(id);
    const version = findVersion(listed, versionId);
    if (!version) return NOTHING;
    const isOpen = id === state.activeId && base?.id === id;
    const current = isOpen ? editor : listed;
    const kept = hasVersionOf(listed, current)
      ? listed
      : addVersion(listed, current, now(), 'Before restore');
    const restored = restoreVersion(kept, version);
    const res = await client.save(id, restored, isOpen ? base.revision : listed.revision);
    const project = stored(res, restored);
    activate(project, { error: null });
    return { editor: project, name: project.name };
  });

  return {
    getState: () => state,
    open, retry, refresh, save, load, create, remove, restore,
  };
}
