import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { defaultFleet } from './useFleet.js';
import { makeScene } from '../engine/sequencer.js';
import {
  DEFAULT_PROJECT_NAME, createProjectVault, createVaultClient,
} from '../engine/projectVault.js';

const browserStorage = () => {
  try { return globalThis.localStorage ?? null; } catch { return null; }
};

/**
 * Saved projects, kept in the backend's Project Vault (issue #101).
 *
 * Holds no show state of its own — it reads and writes the caller's live state,
 * so there's exactly one source of truth for what the designer is editing.
 * The vault logic itself is engine/projectVault.js; this hook connects it to
 * React: it mirrors the vault's state and puts a loaded project in the editor.
 */
export default function useProjects(bindings) {
  const [vaultState, setVaultState] = useState({ status: 'loading', projects: [], activeId: null, error: null });
  const [projectName, setProjectName] = useState(DEFAULT_PROJECT_NAME);
  // True once the vault has decided what the editor shows on startup (a saved
  // project, or the show already on screen), reachable or not.
  const [booted, setBooted] = useState(false);

  const vaultRef = useRef(null);
  if (!vaultRef.current) {
    vaultRef.current = createProjectVault({
      client: createVaultClient(), storage: browserStorage(), onChange: setVaultState,
    });
  }
  const vault = vaultRef.current;

  // Unsaved-changes indicator (issue #132): null until the vault has settled
  // once (open/load/create/restore/save), so the editor never flashes "unsaved"
  // before there's anything to compare against. State, not a ref: isDirty's
  // memo below depends on it, and a ref update alone never triggers a
  // re-render, so isDirty would stay stuck at its last computed value
  // forever after a save, only catching up by accident on some unrelated
  // re-render.
  //
  // Holds the snapshot object itself, not a JSON.stringify of it (issue #132
  // review, Harsh): every setter that can change scenes/dronesList/choreoData/
  // validation goes through useSceneList/useFleet/useShowBuilder, all of
  // which replace the value with a new reference rather than mutating it in
  // place (the undo/redo history in useSceneList/useFleet requires this
  // already). isDirty can therefore compare each field by `!==` instead of by
  // content, which costs nothing proportional to size - at 500-1000 drones a
  // round-trip JSON.stringify of choreoData+validation measured 130-400ms,
  // enough to be felt on every build/validate.
  const [lastSaved, setLastSaved] = useState(null);
  const rememberSaved = useCallback((snap) => { setLastSaved(snap); }, []);

  // Vault operations finish after the render that started them; they read the
  // editor through this so they see its state as of the call, not a stale one.
  const live = useRef({ bindings, projectName });
  useEffect(() => { live.current = { bindings, projectName }; });

  const snapshot = useCallback(() => {
    const { bindings: b, projectName: name } = live.current;
    return {
      name,
      scenes: b.scenes,
      rate: b.rate,
      homeLat: b.homeLat,
      homeLon: b.homeLon,
      dronesList: b.dronesList,
      choreoData: b.choreoData,
      validation: b.validation,
    };
  }, []);

  // Loading a project opens a different show: setScenes/setDronesList clear
  // the undo history (see useSceneList, useFleet).
  const settle = useCallback((result) => {
    const { bindings: b } = live.current;
    const project = result?.editor;
    if (project) {
      const normalized = {
        name: project.name,
        scenes: project.scenes?.length ? project.scenes : [makeScene({ name: 'Scene 1' })],
        rate: project.rate ?? 4,
        dronesList: project.dronesList?.length ? project.dronesList : defaultFleet(6),
        homeLat: project.homeLat ?? 12.9716,
        homeLon: project.homeLon ?? 77.5946,
        choreoData: project.choreoData ?? null,
        validation: project.validation ?? null,
      };
      setProjectName(normalized.name);
      b.setScenes(normalized.scenes);
      b.setRate(normalized.rate);
      b.setDronesList(normalized.dronesList);
      b.setHomeLat(normalized.homeLat);
      b.setHomeLon(normalized.homeLon);
      b.setChoreoData(normalized.choreoData);
      b.setValidation(normalized.validation);
      // This project's own content IS the new unsaved-changes baseline
      // (issue #132) - correct for load/create/restore, and for open()'s
      // non-empty-vault path.
      rememberSaved(normalized);
    } else if (result?.name) {
      setProjectName(result.name);
    }
  }, [rememberSaved]);

  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const snap = snapshot();
    vault.open(snap).then((result) => {
      settle(result);
      // open() returns no editor payload when it just auto-created the
      // vault's first project from whatever was already on screen (empty
      // vault case) - settle() has nothing to derive a baseline from then,
      // so remember the snapshot that was actually passed in and became it.
      if (!result?.editor) rememberSaved(snap);
      setBooted(true);
    });
  }, [vault, snapshot, settle, rememberSaved]);

  // Saving keeps the previous saves too: each one that changed the show is
  // added to the project's version list (issue #100) instead of being lost.
  const saveCurrent = useCallback(() => {
    const snap = snapshot();
    return vault.save(snap).then((result) => {
      settle(result);
      // save() never echoes an editor payload back, even on success - the
      // pre-save snapshot is what ended up persisted either way (updated in
      // place, saved as a brand-new project, or renamed into a conflicted
      // copy, which is the only case result.name overrides it).
      rememberSaved({ ...snap, name: result?.name || snap.name });
      return result;
    });
  }, [vault, snapshot, settle, rememberSaved]);
  const load = useCallback((id) => vault.load(id).then(settle), [vault, settle]);
  const create = useCallback(() => vault.create().then(settle), [vault, settle]);
  const remove = useCallback((id) => vault.remove(id).then(settle), [vault, settle]);
  const restore = useCallback(
    (id, versionId) => vault.restore(id, versionId, snapshot()).then(settle),
    [vault, snapshot, settle],
  );
  const retry = useCallback(() => vault.retry(snapshot()).then(settle), [vault, snapshot, settle]);

  // Unsaved-changes indicator (issue #132). Reactive on the actual saveable
  // fields only - not on bindings as a whole, so it doesn't recompute on
  // every unrelated render, e.g. the playhead advancing during playback.
  // Each field is compared by reference (see lastSaved's comment above),
  // not content, so this costs nothing proportional to show size.
  const isDirty = useMemo(() => {
    if (!booted || lastSaved === null) return false;
    return (
      projectName !== lastSaved.name
      || bindings.scenes !== lastSaved.scenes
      || bindings.rate !== lastSaved.rate
      || bindings.homeLat !== lastSaved.homeLat
      || bindings.homeLon !== lastSaved.homeLon
      || bindings.dronesList !== lastSaved.dronesList
      || bindings.choreoData !== lastSaved.choreoData
      || bindings.validation !== lastSaved.validation
    );
  }, [
    booted, lastSaved, projectName, bindings.scenes, bindings.rate, bindings.homeLat,
    bindings.homeLon, bindings.dronesList, bindings.choreoData, bindings.validation,
  ]);

  return {
    projectsList: vaultState.projects,
    activeProjectId: vaultState.activeId,
    projectName, setProjectName,
    saveCurrent, load, create, remove, restore,
    status: vaultState.status,
    error: vaultState.error,
    booted,
    isDirty,
    retry,
    refresh: vault.refresh,
  };
}
