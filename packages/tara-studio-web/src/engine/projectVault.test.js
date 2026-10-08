import { describe, expect, it } from 'vitest';
import {
  ACTIVE_KEY, DEFAULT_PROJECT_ID, LEGACY_KEY, MIGRATED_KEY,
  VaultError, createProjectVault, createVaultClient, fromWire, toWire,
} from './projectVault.js';
import { MAX_VERSIONS, addVersion } from './projectVersions.js';
import { makeScene } from './sequencer.js';
import { defaultFleet } from './fleetCommands.js';
import { createFakeVaultServer } from './__fixtures__/fakeVaultServer.js';

// Issue #101. Everything here runs against fakeVaultServer.js, which
// fakeVaultServer.test.js holds to a recording of the real backend.
//
// A "browser" below is a vault controller plus its own localStorage and the
// editor state the Studio would hold. Reloading is a new controller over the
// same storage; clearing the cache is new storage; another machine is both.

const memoryStorage = (initial = {}) => {
  const items = new Map(Object.entries(initial));
  return {
    getItem: (k) => (items.has(k) ? items.get(k) : null),
    setItem: (k, v) => { items.set(k, String(v)); },
    has: (k) => items.has(k),
  };
};

const SHOW = { drones: [{ id: 0, waypoints: [{ time: 0, x: 0, y: 0, z: -5, yaw: 0 }, { time: 1, x: 1, y: 0, z: -5, yaw: 0 }] }], _duration: 1 };
const REPORT = { status: 'PASSED', metrics: { min_distance_m: 2 } };

const show = (...names) => ({
  name: 'Untitled Show',
  scenes: names.map((n) => makeScene({ name: n })),
  rate: 4,
  homeLat: 12.9716,
  homeLon: 77.5946,
  dronesList: defaultFleet(3),
  choreoData: null,
  validation: null,
});

/** A Studio tab: the controller, and the editor state its results are applied to. */
function browser(server, storage = memoryStorage(), editor = show('5', '4')) {
  let clock = Date.UTC(2026, 9, 6, 10, 0, 0);
  const tab = {
    storage,
    editor,
    state: null,
    vault: createProjectVault({
      client: createVaultClient({ base: 'http://vault.test', fetchImpl: server.fetch }),
      storage,
      now: () => { clock += 60_000; return clock; },
      onChange: (s) => { tab.state = s; },
    }),
  };
  // What useProjects does with an operation's result.
  const apply = (result) => {
    if (result.editor) {
      const { scenes, rate, homeLat, homeLon, dronesList, choreoData, validation, name } = result.editor;
      tab.editor = { name, scenes, rate, homeLat, homeLon, dronesList, choreoData, validation };
    } else if (result.name) {
      tab.editor = { ...tab.editor, name: result.name };
    }
    return result;
  };
  tab.open = async () => apply(await tab.vault.open(tab.editor));
  tab.save = async () => apply(await tab.vault.save(tab.editor));
  tab.load = async (id) => apply(await tab.vault.load(id));
  tab.create = async () => apply(await tab.vault.create());
  tab.remove = async (id) => apply(await tab.vault.remove(id));
  tab.restore = async (id, versionId) => apply(await tab.vault.restore(id, versionId, tab.editor));
  tab.edit = (patch) => { tab.editor = { ...tab.editor, ...patch }; };
  tab.active = () => tab.state.projects.find((p) => p.id === tab.state.activeId);
  return tab;
}

const sceneNames = (project) => project.scenes.map((s) => s.name);
const requestsTo = (server, method) => server.requests.filter((r) => r.method === method);

describe('wire format', () => {
  it('round-trips a project through the backend shape', () => {
    const project = addVersion({ ...show('a', 'b'), name: 'Diwali', choreoData: SHOW, validation: REPORT }, show('a', 'b'), 1);
    const wire = toWire(project);
    expect(wire.document).toEqual({
      scenes: project.scenes, dronesList: project.dronesList, rate: 4, homeLat: 12.9716, homeLon: 77.5946,
    });
    expect(wire.show).toBe(SHOW);

    const back = fromWire({ id: 'p1', revision: 3, created_at: 't0', updated_at: 't1', has_show: true, ...wire });
    expect(back).toMatchObject({
      id: 'p1', name: 'Diwali', revision: 3, hasShow: true, choreoData: SHOW, validation: REPORT,
      scenes: project.scenes, dronesList: project.dronesList, rate: 4,
    });
    expect(back.versions).toEqual(project.versions);
  });

  it('never sends a blank name, a malformed history entry, or more history than the vault keeps', () => {
    const versions = [
      ...Array.from({ length: MAX_VERSIONS + 5 }, (_, i) => ({ id: `v-${i}`, savedAt: '2026-10-06T10:00:00.000Z' })),
    ];
    versions.splice(1, 0, { savedAt: 'no id' }, null);
    const wire = toWire({ ...show('a'), name: '   ', versions });
    expect(wire.name).toBe('Untitled Show');
    expect(wire.versions).toHaveLength(MAX_VERSIONS);
    expect(wire.versions.every((v) => v.id)).toBe(true);
  });
});

describe('vault client', () => {
  it('turns an unreachable backend into an offline VaultError', async () => {
    const server = createFakeVaultServer();
    server.down = true;
    const client = createVaultClient({ base: 'http://vault.test', fetchImpl: server.fetch });
    const error = await client.list().catch((e) => e);
    expect(error).toBeInstanceOf(VaultError);
    expect(error.offline).toBe(true);
    expect(error.message).toMatch(/cannot be reached/);
  });

  it('carries the backend error code and message', async () => {
    const fetchImpl = async () => ({
      status: 409, ok: false,
      json: async () => ({ detail: { code: 'revision_conflict', message: 'Saved elsewhere.', current_revision: 4 } }),
    });
    const error = await createVaultClient({ base: '', fetchImpl }).save('p1', show('a'), 1).catch((e) => e);
    expect(error).toMatchObject({ status: 409, code: 'revision_conflict', message: 'Saved elsewhere.', offline: false });
    expect(error.detail.current_revision).toBe(4);
  });

  it('survives an error response that is not JSON', async () => {
    const fetchImpl = async () => ({ status: 503, ok: false, json: async () => { throw new SyntaxError('html'); } });
    const error = await createVaultClient({ base: '', fetchImpl }).list().catch((e) => e);
    expect(error).toMatchObject({ status: 503, code: 'http', offline: true });
  });
});

describe('opening the vault', () => {
  it('makes the show on screen the first project of an empty vault', async () => {
    const server = createFakeVaultServer();
    const tab = browser(server);
    const result = await tab.open();

    expect(result.editor).toBeNull();            // the editor already shows it
    expect(tab.state.status).toBe('ready');
    expect(tab.state.activeId).toBe(DEFAULT_PROJECT_ID);
    expect(tab.state.projects).toHaveLength(1);
    expect(server.stored(DEFAULT_PROJECT_ID).name).toBe('Untitled Show');
    expect(server.stored(DEFAULT_PROJECT_ID).document.scenes.map((s) => s.name)).toEqual(['5', '4']);
  });

  it('reopens the saved show after a reload', async () => {
    const server = createFakeVaultServer();
    const first = browser(server);
    await first.open();
    first.edit({ name: 'Diwali', scenes: show('a', 'b', 'c').scenes, choreoData: SHOW, validation: REPORT });
    await first.save();

    const reloaded = browser(server, first.storage);   // boots with the demo show again
    const result = await reloaded.open();

    expect(result.editor).not.toBeNull();
    expect(reloaded.editor.name).toBe('Diwali');
    expect(sceneNames(reloaded.editor)).toEqual(['a', 'b', 'c']);
    expect(reloaded.editor.choreoData).toEqual({ version: 1, ...SHOW });
    expect(reloaded.editor.validation).toEqual(REPORT);
    expect(reloaded.state.activeId).toBe(DEFAULT_PROJECT_ID);
  });

  it('reopens the project that was open last, not just the first one', async () => {
    const server = createFakeVaultServer();
    const first = browser(server);
    await first.open();
    await first.create();
    first.edit({ name: 'Second show', scenes: show('x').scenes });
    await first.save();
    const secondId = first.state.activeId;
    expect(first.storage.getItem(ACTIVE_KEY)).toBe(secondId);

    const reloaded = browser(server, first.storage);
    await reloaded.open();
    expect(reloaded.state.activeId).toBe(secondId);
    expect(reloaded.editor.name).toBe('Second show');
  });

  it('keeps projects and their save history when the browser cache is cleared', async () => {
    const server = createFakeVaultServer();
    const first = browser(server);
    await first.open();
    await first.save();
    first.edit({ scenes: show('a', 'b', 'c').scenes });
    await first.save();

    const cleared = browser(server, memoryStorage());
    await cleared.open();
    expect(sceneNames(cleared.editor)).toEqual(['a', 'b', 'c']);
    expect(cleared.active().versions.map((v) => v.scenes.length)).toEqual([3, 2]);
  });

  it('opens on another machine what was saved on this one', async () => {
    const server = createFakeVaultServer();
    const laptop = browser(server);
    await laptop.open();
    laptop.edit({ name: 'Ganesh Chaturthi', scenes: show('modak').scenes, choreoData: SHOW });
    await laptop.save();

    const desktop = browser(server, memoryStorage(), show('5', '4'));
    await desktop.open();
    expect(desktop.editor.name).toBe('Ganesh Chaturthi');
    expect(sceneNames(desktop.editor)).toEqual(['modak']);
    expect(desktop.editor.choreoData.drones).toHaveLength(1);
  });

  it('falls back to the first project when the remembered one is gone', async () => {
    const server = createFakeVaultServer();
    const first = browser(server);
    await first.open();
    const storage = memoryStorage({ [ACTIVE_KEY]: 'deleted-long-ago' });
    const tab = browser(server, storage);
    await tab.open();
    expect(tab.state.activeId).toBe(DEFAULT_PROJECT_ID);
  });

  it('reports an unreachable backend instead of throwing, and leaves the editor alone', async () => {
    const server = createFakeVaultServer();
    server.down = true;
    const tab = browser(server);
    const result = await tab.open();

    expect(result.editor).toBeNull();
    expect(tab.state.status).toBe('offline');
    expect(tab.state.error).toMatch(/cannot be reached/);
    expect(tab.state.projects).toEqual([]);
    expect(sceneNames(tab.editor)).toEqual(['5', '4']);
  });

  it('works without localStorage', async () => {
    const server = createFakeVaultServer();
    const broken = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } };
    for (const storage of [null, broken]) {
      const tab = browser(server, storage);
      await tab.open();
      await tab.save();
      expect(tab.state.status).toBe('ready');
      expect(tab.state.error).toBeNull();
    }
  });
});

describe('coming back from offline', () => {
  it('saves what was built offline as a project of its own, beside what the vault already had', async () => {
    const server = createFakeVaultServer();
    const earlier = browser(server);
    await earlier.open();
    earlier.edit({ name: 'Already in the vault' });
    await earlier.save();

    server.down = true;
    const tab = browser(server, memoryStorage(), show('built', 'offline'));
    await tab.open();
    tab.edit({ name: 'Offline work' });
    await tab.save();
    expect(tab.state.status).toBe('offline');
    expect(tab.state.error).toMatch(/cannot be reached/);

    server.down = false;
    await tab.save();
    expect(tab.state.status).toBe('ready');
    expect(tab.state.error).toBeNull();
    expect(server.ids()).toHaveLength(2);
    expect(server.stored(DEFAULT_PROJECT_ID).name).toBe('Already in the vault');
    expect(tab.active().name).toBe('Offline work');
    expect(sceneNames(tab.active())).toEqual(['built', 'offline']);
  });

  it('retry reconnects without replacing what is in the editor', async () => {
    const server = createFakeVaultServer();
    const earlier = browser(server);
    await earlier.open();

    server.down = true;
    const tab = browser(server, memoryStorage(), show('keep', 'me'));
    await tab.open();
    server.down = false;
    const result = await tab.vault.retry(tab.editor);

    expect(result.editor).toBeNull();
    expect(tab.state.status).toBe('ready');
    expect(tab.state.projects).toHaveLength(1);
    expect(tab.state.activeId).toBeNull();
    expect(sceneNames(tab.editor)).toEqual(['keep', 'me']);
  });
});

describe('saving', () => {
  const opened = async (server = createFakeVaultServer()) => {
    const tab = browser(server);
    await tab.open();
    return { server, tab };
  };

  it('stores the edit and adds it to the save history', async () => {
    const { server, tab } = await opened();
    await tab.save();
    tab.edit({ scenes: show('a', 'b', 'c').scenes });
    await tab.save();

    const stored = server.stored(DEFAULT_PROJECT_ID);
    expect(stored.revision).toBe(3);
    expect(stored.document.scenes).toHaveLength(3);
    expect(stored.versions.map((v) => [v.label, v.scenes.length])).toEqual([['Saved', 3], ['Saved', 2]]);
    expect(tab.active().versions).toEqual(stored.versions);
  });

  it('sends nothing when nothing changed', async () => {
    const { server, tab } = await opened();
    await tab.save();
    const before = requestsTo(server, 'PUT').length;
    await tab.save();
    await tab.save();
    expect(requestsTo(server, 'PUT')).toHaveLength(before);
    expect(server.stored(DEFAULT_PROJECT_ID).versions).toHaveLength(1);
  });

  it('saves a rename without adding a history entry', async () => {
    const { server, tab } = await opened();
    await tab.save();
    tab.edit({ name: 'Republic Day' });
    await tab.save();
    expect(server.stored(DEFAULT_PROJECT_ID).name).toBe('Republic Day');
    expect(server.stored(DEFAULT_PROJECT_ID).versions).toHaveLength(1);
  });

  it('runs two saves fired back to back in order, without conflicting with itself', async () => {
    // Renaming the project and clicking Save: the name field's blur and the
    // button's click both save, in the same tick.
    const { server, tab } = await opened();
    tab.edit({ name: 'Renamed' });
    const blur = tab.vault.save(tab.editor);
    tab.edit({ scenes: show('a', 'b', 'c').scenes });
    const click = tab.vault.save(tab.editor);
    await Promise.all([blur, click]);

    expect(tab.state.error).toBeNull();
    expect(server.ids()).toEqual([DEFAULT_PROJECT_ID]);
    const stored = server.stored(DEFAULT_PROJECT_ID);
    expect(stored.name).toBe('Renamed');
    expect(stored.document.scenes).toHaveLength(3);
    expect(stored.revision).toBe(3);
  });

  it('stores the built show with the project, and re-sends it only when it changed', async () => {
    const { server, tab } = await opened();
    tab.edit({ choreoData: SHOW, validation: REPORT });
    await tab.save();
    expect(tab.active().hasShow).toBe(true);
    expect(server.stored(DEFAULT_PROJECT_ID).show).toEqual({ version: 1, ...SHOW });
    expect(server.stored(DEFAULT_PROJECT_ID).validation).toEqual(REPORT);

    const before = requestsTo(server, 'PUT').length;
    await tab.save();
    expect(requestsTo(server, 'PUT')).toHaveLength(before);

    tab.edit({ choreoData: { ...SHOW, _duration: 2 } });   // rebuilt
    await tab.save();
    expect(server.stored(DEFAULT_PROJECT_ID).show._duration).toBe(2);
  });

  it('saves the design and says so when the built show is turned down', async () => {
    const { server, tab } = await opened();
    const broken = { drones: [{ id: 0, waypoints: [{ time: 0, x: 0, y: 0, z: -5, yaw: 400 }] }] };
    tab.edit({ scenes: show('a', 'b', 'c').scenes, choreoData: broken });
    await tab.save();

    expect(server.stored(DEFAULT_PROJECT_ID).document.scenes).toHaveLength(3);
    expect(server.stored(DEFAULT_PROJECT_ID).show).toBeNull();
    expect(tab.active().hasShow).toBe(false);
    expect(tab.state.status).toBe('ready');
  });

  it('keeps both copies when the project was saved somewhere else in the meantime', async () => {
    const server = createFakeVaultServer();
    const a = browser(server);
    await a.open();
    a.edit({ name: 'Diwali' });
    await a.save();

    const b = browser(server, memoryStorage());
    await b.open();
    a.edit({ scenes: show('from', 'A').scenes });
    await a.save();

    b.edit({ scenes: show('from', 'B', 'unaware', 'of', 'A').scenes });
    const result = await b.save();

    // A's save is untouched...
    expect(server.stored(DEFAULT_PROJECT_ID).document.scenes.map((s) => s.name)).toEqual(['from', 'A']);
    // ...and B's edits are a project of their own, which B now has open.
    expect(result.name).toBe('Diwali (conflicted copy)');
    expect(b.editor.name).toBe('Diwali (conflicted copy)');
    const copyId = b.state.activeId;
    expect(copyId).not.toBe(DEFAULT_PROJECT_ID);
    expect(server.stored(copyId).document.scenes).toHaveLength(5);
    expect(b.state.error).toMatch(/saved somewhere else/);
    expect(b.storage.getItem(ACTIVE_KEY)).toBe(copyId);

    // B carries on in the copy without further conflicts.
    b.edit({ scenes: show('more').scenes });
    await b.save();
    expect(server.stored(copyId).document.scenes).toHaveLength(1);
    expect(server.ids()).toHaveLength(2);
  });

  it('still detects the conflict after the project list was refreshed', async () => {
    const server = createFakeVaultServer();
    const a = browser(server);
    await a.open();
    const b = browser(server, memoryStorage());
    await b.open();
    a.edit({ scenes: show('from', 'A').scenes });
    await a.save();

    await b.vault.refresh();   // B opens the vault page and sees A's newer save listed
    b.edit({ scenes: show('from', 'B').scenes });
    await b.save();

    expect(server.stored(DEFAULT_PROJECT_ID).document.scenes.map((s) => s.name)).toEqual(['from', 'A']);
    expect(b.state.activeId).not.toBe(DEFAULT_PROJECT_ID);
  });

  it('saves again as a new project when the open one was deleted somewhere else', async () => {
    const server = createFakeVaultServer();
    const a = browser(server);
    await a.open();
    await a.create();
    const doomed = a.state.activeId;
    a.edit({ name: 'Still being edited', scenes: show('wip').scenes });

    server.handle('DELETE', `/api/projects/${doomed}`);
    await a.save();

    expect(server.stored(doomed)).toBeNull();
    expect(a.state.activeId).not.toBe(doomed);
    expect(server.stored(a.state.activeId).name).toBe('Still being edited');
    expect(a.state.projects.map((p) => p.id)).not.toContain(doomed);
    expect(a.state.error).toMatch(/deleted/);
  });
});

describe('save history and restore', () => {
  const withHistory = async () => {
    const server = createFakeVaultServer();
    const tab = browser(server);
    await tab.open();
    await tab.save();                                           // 2 scenes
    tab.edit({ scenes: show('a', 'b', 'c', 'd').scenes, choreoData: SHOW, validation: REPORT });
    await tab.save();                                           // 4 scenes, built
    return { server, tab };
  };

  it('restores an earlier save, keeping unsaved edits as a version of their own', async () => {
    const { server, tab } = await withHistory();
    tab.edit({ scenes: show('unsaved').scenes });
    const oldest = tab.active().versions.at(-1);

    const result = await tab.restore(DEFAULT_PROJECT_ID, oldest.id);

    expect(sceneNames(result.editor)).toEqual(['5', '4']);
    // The built show belonged to the state that was replaced.
    expect(tab.editor.choreoData).toBeNull();
    expect(tab.editor.validation).toBeNull();
    const stored = server.stored(DEFAULT_PROJECT_ID);
    expect(stored.document.scenes.map((s) => s.name)).toEqual(['5', '4']);
    expect(stored.show).toBeNull();
    expect(stored.versions.map((v) => [v.label, v.scenes.length])).toEqual([
      ['Before restore', 1], ['Saved', 4], ['Saved', 2],
    ]);
  });

  it('adds no "Before restore" entry when the current state is already saved', async () => {
    const { server, tab } = await withHistory();
    await tab.restore(DEFAULT_PROJECT_ID, tab.active().versions.at(-1).id);
    expect(server.stored(DEFAULT_PROJECT_ID).versions.map((v) => v.label)).toEqual(['Saved', 'Saved']);
  });

  it('keeps the history across a reload, and it can be restored from again', async () => {
    const { server, tab } = await withHistory();
    await tab.restore(DEFAULT_PROJECT_ID, tab.active().versions.at(-1).id);

    const reloaded = browser(server, tab.storage);
    await reloaded.open();
    expect(sceneNames(reloaded.editor)).toEqual(['5', '4']);
    expect(reloaded.active().versions).toHaveLength(2);

    await reloaded.restore(DEFAULT_PROJECT_ID, reloaded.active().versions[0].id);
    expect(sceneNames(reloaded.editor)).toEqual(['a', 'b', 'c', 'd']);
    expect(server.stored(DEFAULT_PROJECT_ID).document.scenes).toHaveLength(4);
  });

  it('restores a project that is not the open one, and opens it', async () => {
    const { server, tab } = await withHistory();
    await tab.create();
    const other = tab.state.activeId;

    const target = tab.state.projects.find((p) => p.id === DEFAULT_PROJECT_ID);
    await tab.restore(DEFAULT_PROJECT_ID, target.versions.at(-1).id);

    expect(tab.state.activeId).toBe(DEFAULT_PROJECT_ID);
    expect(sceneNames(tab.editor)).toEqual(['5', '4']);
    expect(server.stored(other).document.scenes).toHaveLength(1);   // the other project is untouched
    expect(server.stored(DEFAULT_PROJECT_ID).versions.map((v) => v.label)).toEqual(['Saved', 'Saved']);
  });

  it('does nothing for a version that does not exist', async () => {
    const { server, tab } = await withHistory();
    const before = server.stored(DEFAULT_PROJECT_ID);
    const result = await tab.restore(DEFAULT_PROJECT_ID, 'v-not-there');
    expect(result.editor).toBeNull();
    expect(server.stored(DEFAULT_PROJECT_ID)).toEqual(before);
  });

  it('reports a restore the vault refuses and leaves the editor as it was', async () => {
    const { server, tab } = await withHistory();
    const other = browser(server, memoryStorage());
    await other.open();
    other.edit({ scenes: show('elsewhere').scenes });
    await other.save();

    tab.edit({ scenes: show('unsaved').scenes });
    const result = await tab.restore(DEFAULT_PROJECT_ID, tab.active().versions.at(-1).id);
    expect(result.editor).toBeNull();
    expect(sceneNames(tab.editor)).toEqual(['unsaved']);
    expect(tab.state.error).toMatch(/saved somewhere else/);
    expect(server.stored(DEFAULT_PROJECT_ID).document.scenes.map((s) => s.name)).toEqual(['elsewhere']);
  });

  it('keeps the newest saves once the history is full', async () => {
    const server = createFakeVaultServer();
    const tab = browser(server);
    await tab.open();
    for (let i = 0; i < MAX_VERSIONS + 3; i += 1) {
      tab.edit({ scenes: show(`take-${i}`).scenes });
      await tab.save();
    }
    const { versions } = server.stored(DEFAULT_PROJECT_ID);
    expect(versions).toHaveLength(MAX_VERSIONS);
    expect(versions[0].scenes[0].name).toBe(`take-${MAX_VERSIONS + 2}`);
    expect(tab.state.error).toBeNull();
  });
});

describe('load, create and delete', () => {
  const twoProjects = async () => {
    const server = createFakeVaultServer();
    const tab = browser(server);
    await tab.open();
    tab.edit({ name: 'First', choreoData: SHOW });
    await tab.save();
    await tab.create();
    return { server, tab, second: tab.state.activeId };
  };

  it('creates an empty show in the vault and opens it', async () => {
    const { server, tab, second } = await twoProjects();
    expect(tab.editor.name).toBe('New Show');
    expect(sceneNames(tab.editor)).toEqual(['Scene 1']);
    expect(tab.editor.dronesList).toHaveLength(6);
    expect(server.stored(second).revision).toBe(1);
    expect(tab.state.projects.map((p) => p.name)).toEqual(['First', 'New Show']);
  });

  it('loads a project with its built show, which the list alone does not carry', async () => {
    const { server, tab } = await twoProjects();
    const other = browser(server, memoryStorage({ [ACTIVE_KEY]: tab.state.activeId }));
    await other.open();
    const listed = other.state.projects.find((p) => p.id === DEFAULT_PROJECT_ID);
    expect(listed.hasShow).toBe(true);
    expect(listed.choreoData).toBeNull();

    await other.load(DEFAULT_PROJECT_ID);
    expect(other.editor.name).toBe('First');
    expect(other.editor.choreoData.drones).toHaveLength(1);
    expect(other.state.activeId).toBe(DEFAULT_PROJECT_ID);
  });

  it('reports loading a project that was deleted elsewhere and drops it from the list', async () => {
    const { server, tab, second } = await twoProjects();
    server.handle('DELETE', `/api/projects/${DEFAULT_PROJECT_ID}`);
    const result = await tab.load(DEFAULT_PROJECT_ID);
    expect(result.editor).toBeNull();
    expect(tab.state.projects.map((p) => p.id)).toEqual([second]);
    expect(tab.state.error).toBeTruthy();
    expect(tab.state.activeId).toBe(second);
  });

  it('deletes a project that is not open without touching the editor', async () => {
    const { server, tab, second } = await twoProjects();
    const result = await tab.remove(DEFAULT_PROJECT_ID);
    expect(result.editor).toBeNull();
    expect(server.ids()).toEqual([second]);
    expect(tab.state.projects.map((p) => p.id)).toEqual([second]);
  });

  it('moves the editor to another project when the open one is deleted', async () => {
    const { server, tab, second } = await twoProjects();
    await tab.remove(second);
    expect(server.ids()).toEqual([DEFAULT_PROJECT_ID]);
    expect(tab.state.activeId).toBe(DEFAULT_PROJECT_ID);
    expect(tab.editor.name).toBe('First');
    expect(tab.editor.choreoData.drones).toHaveLength(1);
  });

  it('never deletes the last project', async () => {
    const server = createFakeVaultServer();
    const tab = browser(server);
    await tab.open();
    await tab.remove(DEFAULT_PROJECT_ID);
    expect(server.ids()).toEqual([DEFAULT_PROJECT_ID]);
    expect(requestsTo(server, 'DELETE')).toHaveLength(0);
  });

  it('shows projects another browser added once the list is refreshed', async () => {
    const { server, tab } = await twoProjects();
    const other = browser(server, memoryStorage());
    await other.open();
    await tab.create();
    expect(other.state.projects).toHaveLength(2);
    await other.vault.refresh();
    expect(other.state.projects).toHaveLength(3);
  });
});

describe('importing projects saved before the vault', () => {
  // What useProjects wrote to localStorage up to issue #100.
  const legacyVault = () => {
    const first = { ...show('one', 'two'), id: 'default-proj', name: 'Untitled Show' };
    const built = addVersion(
      { ...show('a', 'b', 'c'), id: 'proj-1759400000000', name: 'Navratri', choreoData: SHOW, validation: REPORT },
      show('a', 'b', 'c'), Date.UTC(2026, 9, 3, 10),
    );
    return [first, built];
  };
  const legacyStorage = (projects = legacyVault()) => memoryStorage({ [LEGACY_KEY]: JSON.stringify(projects) });

  it('brings them in with their history and built show, and opens the first', async () => {
    const server = createFakeVaultServer();
    const tab = browser(server, legacyStorage());
    await tab.open();

    expect(server.ids()).toEqual(['default-proj', 'proj-1759400000000']);
    const navratri = server.stored('proj-1759400000000');
    expect(navratri.name).toBe('Navratri');
    expect(navratri.versions).toHaveLength(1);
    expect(navratri.show.drones).toHaveLength(1);
    expect(navratri.validation).toEqual(REPORT);

    // The imported project is opened, not overwritten with the demo show.
    expect(sceneNames(tab.editor)).toEqual(['one', 'two']);
    expect(tab.state.error).toBeNull();
  });

  it('leaves the old localStorage entry in place and imports only once', async () => {
    const server = createFakeVaultServer();
    const storage = legacyStorage();
    const before = storage.getItem(LEGACY_KEY);
    await browser(server, storage).open();
    expect(storage.getItem(LEGACY_KEY)).toBe(before);
    expect(storage.has(MIGRATED_KEY)).toBe(true);

    const imports = requestsTo(server, 'POST').filter((r) => r.path.endsWith('/import')).length;
    await browser(server, storage).open();
    expect(requestsTo(server, 'POST').filter((r) => r.path.endsWith('/import'))).toHaveLength(imports);
    expect(server.ids()).toHaveLength(2);
  });

  it('keeps two browsers\' different "default-proj" projects side by side', async () => {
    const server = createFakeVaultServer();
    await browser(server, legacyStorage([{ ...show('laptop'), id: 'default-proj', name: 'Untitled Show' }])).open();
    await browser(server, legacyStorage([{ ...show('desktop'), id: 'default-proj', name: 'Untitled Show' }])).open();

    const names = server.ids().map((id) => server.stored(id).document.scenes[0].name).sort();
    expect(names).toEqual(['desktop', 'laptop']);
  });

  it('does not duplicate projects if the migration marker is lost', async () => {
    const server = createFakeVaultServer();
    const saved = legacyStorage().getItem(LEGACY_KEY);
    await browser(server, memoryStorage({ [LEGACY_KEY]: saved })).open();
    await browser(server, memoryStorage({ [LEGACY_KEY]: saved })).open();   // same old projects, marker gone
    expect(server.ids()).toHaveLength(2);
  });

  it('tries again later when the backend was down, without marking anything migrated', async () => {
    const server = createFakeVaultServer();
    server.down = true;
    const storage = legacyStorage();
    const tab = browser(server, storage);
    await tab.open();
    expect(tab.state.status).toBe('offline');
    expect(storage.has(MIGRATED_KEY)).toBe(false);

    server.down = false;
    await browser(server, storage).open();
    expect(server.ids()).toHaveLength(2);
    expect(storage.has(MIGRATED_KEY)).toBe(true);
  });

  it('imports the rest and says which project the vault refused', async () => {
    const server = createFakeVaultServer();
    const tooBig = { ...show('huge'), id: 'proj-huge', name: 'Too many drones', dronesList: defaultFleet(1001) };
    const storage = legacyStorage([...legacyVault(), tooBig]);
    const tab = browser(server, storage);
    await tab.open();

    expect(server.ids()).toEqual(['default-proj', 'proj-1759400000000']);
    expect(tab.state.status).toBe('ready');
    expect(tab.state.error).toMatch(/Too many drones/);
    expect(tab.state.error).toMatch(/still in this browser/);
    expect(storage.has(MIGRATED_KEY)).toBe(false);   // so it is retried
  });

  it('ignores a corrupt localStorage entry', async () => {
    const server = createFakeVaultServer();
    const tab = browser(server, memoryStorage({ [LEGACY_KEY]: '{"not": "a list"' }));
    await tab.open();
    expect(tab.state.status).toBe('ready');
    expect(server.ids()).toEqual([DEFAULT_PROJECT_ID]);
    expect(tab.state.error).toBeNull();
  });
});
