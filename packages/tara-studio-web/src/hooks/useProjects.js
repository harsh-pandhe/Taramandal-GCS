import { useState, useEffect, useCallback } from 'react';
import { defaultFleet } from './useFleet.js';
import { makeScene } from '../engine/sequencer.js';
import { addVersion, findVersion, hasVersionOf, restoreVersion } from '../engine/projectVersions.js';

const STORAGE_KEY = 'taramandal_projects';

const blankProject = (id, name) => ({
  id,
  name,
  scenes: [makeScene({ name: 'Scene 1' })],
  rate: 4,
  homeLat: 12.9716,
  homeLon: 77.5946,
  dronesList: defaultFleet(6),
  choreoData: null,
  validation: null,
});

const loadStored = () => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    // Guard against a corrupted or older-format entry: a bad value here would
    // otherwise throw on first render and blank the whole app.
    if (Array.isArray(parsed) && parsed.length && parsed.every((p) => p && p.id)) {
      return parsed;
    }
  } catch {
    // fall through to a fresh vault
  }
  return [blankProject('default-proj', 'Untitled Show')];
};

/**
 * Saved projects, persisted to localStorage.
 *
 * Holds no show state of its own — it reads and writes the caller's live state,
 * so there's exactly one source of truth for what the designer is editing.
 */
export default function useProjects(bindings) {
  const [projectsList, setProjectsList] = useState(loadStored);
  const [activeProjectId, setActiveProjectId] = useState(() => loadStored()[0].id);
  const [projectName, setProjectName] = useState(() => loadStored()[0].name);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(projectsList));
    } catch {
      // Quota exceeded (a big built show can be megabytes). Losing the autosave
      // is survivable; breaking the render is not.
    }
  }, [projectsList]);

  const snapshot = useCallback(() => ({
    name: projectName,
    scenes: bindings.scenes,
    rate: bindings.rate,
    homeLat: bindings.homeLat,
    homeLon: bindings.homeLon,
    dronesList: bindings.dronesList,
    choreoData: bindings.choreoData,
    validation: bindings.validation,
  }), [projectName, bindings]);

  // Saving keeps the previous saves too: each one that changed the show is
  // added to the project's version list (issue #100) instead of being lost.
  const saveCurrent = useCallback(() => {
    const state = snapshot();
    const now = Date.now();
    setProjectsList((prev) => prev.map((p) => (
      p.id === activeProjectId ? addVersion({ ...p, ...state }, state, now) : p
    )));
  }, [activeProjectId, snapshot]);

  // Puts a project back to one of its saved versions and opens it. If the
  // state being replaced is not already in the list (unsaved edits), it is
  // saved as a version first, so a restore never loses work.
  const restore = useCallback((id, versionId) => {
    const proj = projectsList.find((p) => p.id === id);
    const version = findVersion(proj, versionId);
    if (!version) return;
    const current = id === activeProjectId ? snapshot() : proj;
    const kept = hasVersionOf(proj, current)
      ? proj
      : addVersion(proj, current, Date.now(), 'Before restore');
    const restored = restoreVersion(kept, version);
    setProjectsList((prev) => prev.map((p) => (p.id === id ? restored : p)));
    setActiveProjectId(id);
    setProjectName(restored.name);
    bindings.setScenes(restored.scenes?.length ? restored.scenes : [makeScene({ name: 'Scene 1' })]);
    bindings.setRate(restored.rate ?? 4);
    bindings.setDronesList(restored.dronesList?.length ? restored.dronesList : defaultFleet(6));
    bindings.setHomeLat(restored.homeLat ?? 12.9716);
    bindings.setHomeLon(restored.homeLon ?? 77.5946);
    bindings.setChoreoData(null);
    bindings.setValidation(null);
  }, [projectsList, activeProjectId, snapshot, bindings]);

  const load = useCallback((id) => {
    const proj = projectsList.find((p) => p.id === id);
    if (!proj) return;
    setActiveProjectId(id);
    setProjectName(proj.name);
    bindings.setScenes(proj.scenes?.length ? proj.scenes : [makeScene({ name: 'Scene 1' })]);
    bindings.setRate(proj.rate ?? 4);
    bindings.setDronesList(proj.dronesList?.length ? proj.dronesList : defaultFleet(6));
    bindings.setHomeLat(proj.homeLat ?? 12.9716);
    bindings.setHomeLon(proj.homeLon ?? 77.5946);
    bindings.setChoreoData(proj.choreoData ?? null);
    bindings.setValidation(proj.validation ?? null);
  }, [projectsList, bindings]);

  const create = useCallback(() => {
    const proj = blankProject(`proj-${Date.now()}`, 'New Show');
    setProjectsList((prev) => [...prev, proj]);
    setActiveProjectId(proj.id);
    setProjectName(proj.name);
    bindings.setScenes(proj.scenes);
    bindings.setRate(proj.rate);
    bindings.setDronesList(proj.dronesList);
    bindings.setChoreoData(null);
    bindings.setValidation(null);
  }, [bindings]);

  const remove = useCallback((id) => {
    setProjectsList((prev) => {
      if (prev.length <= 1) return prev;
      const remaining = prev.filter((p) => p.id !== id);
      // Deleting the open project has to move the editor somewhere valid,
      // otherwise the sidebar keeps showing a project that no longer exists.
      if (id === activeProjectId && remaining.length) {
        const next = remaining[0];
        setActiveProjectId(next.id);
        setProjectName(next.name);
        bindings.setScenes(next.scenes?.length ? next.scenes : [makeScene({ name: 'Scene 1' })]);
        bindings.setRate(next.rate ?? 4);
        bindings.setDronesList(next.dronesList?.length ? next.dronesList : defaultFleet(6));
        bindings.setChoreoData(next.choreoData ?? null);
        bindings.setValidation(next.validation ?? null);
      }
      return remaining;
    });
  }, [activeProjectId, bindings]);

  return {
    projectsList, activeProjectId, projectName, setProjectName,
    saveCurrent, load, create, remove, restore,
  };
}
