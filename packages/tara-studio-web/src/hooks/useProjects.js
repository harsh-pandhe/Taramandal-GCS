import { useState, useEffect, useCallback, useRef } from 'react';
import { defaultFleet } from './useFleet.js';
import { makeScene, MIN_CERTIFIABLE_RATE_HZ } from '../engine/sequencer.js';
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
      setProjectName(project.name);
      b.setScenes(project.scenes?.length ? project.scenes : [makeScene({ name: 'Scene 1' })]);
      // issue #160: only the *default* for a project saved without its own
      // rate - an existing saved project keeps whatever rate it was built
      // at, same as every other field here.
      b.setRate(project.rate ?? MIN_CERTIFIABLE_RATE_HZ);
      b.setDronesList(project.dronesList?.length ? project.dronesList : defaultFleet(6));
      b.setHomeLat(project.homeLat ?? 12.9716);
      b.setHomeLon(project.homeLon ?? 77.5946);
      b.setChoreoData(project.choreoData ?? null);
      b.setValidation(project.validation ?? null);
    } else if (result?.name) {
      setProjectName(result.name);
    }
  }, []);

  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    vault.open(snapshot()).then((result) => { settle(result); setBooted(true); });
  }, [vault, snapshot, settle]);

  // Saving keeps the previous saves too: each one that changed the show is
  // added to the project's version list (issue #100) instead of being lost.
  const saveCurrent = useCallback(() => vault.save(snapshot()).then(settle), [vault, snapshot, settle]);
  const load = useCallback((id) => vault.load(id).then(settle), [vault, settle]);
  const create = useCallback(() => vault.create().then(settle), [vault, settle]);
  const remove = useCallback((id) => vault.remove(id).then(settle), [vault, settle]);
  const restore = useCallback(
    (id, versionId) => vault.restore(id, versionId, snapshot()).then(settle),
    [vault, snapshot, settle],
  );
  const retry = useCallback(() => vault.retry(snapshot()).then(settle), [vault, snapshot, settle]);

  return {
    projectsList: vaultState.projects,
    activeProjectId: vaultState.activeId,
    projectName, setProjectName,
    saveCurrent, load, create, remove, restore,
    status: vaultState.status,
    error: vaultState.error,
    booted,
    retry,
    refresh: vault.refresh,
  };
}
