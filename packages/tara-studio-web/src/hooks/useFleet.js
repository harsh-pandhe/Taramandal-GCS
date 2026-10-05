import { useCallback } from 'react';
import { fleetCommands } from '../engine/fleetCommands.js';

export { makeDrone, defaultFleet } from '../engine/fleetCommands.js';

/**
 * The vehicle list and its editing operations.
 *
 * The list lives in the Studio document; every edit is a command run through
 * `history` (see useStudioHistory), so it is undoable alongside scene edits,
 * and a bulk edit of any number of vehicles is a single undo step (#102).
 */
export default function useFleet(history) {
  const { doc, execute, reset } = history;

  // Replaces the whole fleet and clears the history: loading a project opens
  // a different show rather than editing this one.
  const setDronesList = useCallback((next) => {
    reset((prev) => ({
      ...prev,
      dronesList: typeof next === 'function' ? next(prev.dronesList) : next,
    }));
  }, [reset]);

  const addDrone = useCallback(() => execute(fleetCommands.add()), [execute]);
  const removeDrone = useCallback((id) => execute(fleetCommands.remove(id)), [execute]);
  const updateDrone = useCallback((id, field, value) => execute(fleetCommands.update(id, field, value)), [execute]);
  const bulkUpdate = useCallback((ids, patch) => execute(fleetCommands.bulkUpdate(ids, patch)), [execute]);
  const setCount = useCallback((count) => execute(fleetCommands.setCount(count)), [execute]);

  return { dronesList: doc.dronesList, setDronesList, addDrone, removeDrone, updateDrone, bulkUpdate, setCount };
}
