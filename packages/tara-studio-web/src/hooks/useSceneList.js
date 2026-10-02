import { useState, useCallback, useRef } from 'react';
import { sceneCommands } from '../engine/commands.js';

/**
 * The scene keyframe list and its editing operations: add, patch, duplicate,
 * delete, reorder (buttons) and drag-and-drop reorder.
 *
 * The list lives in the Studio document; every operation here is a command run
 * through `history` (see useStudioHistory), so each one is undoable.
 */
export default function useSceneList(history) {
  const { doc, execute, reset } = history;
  const { scenes } = doc;
  const [dragIndex, setDragIndex] = useState(null);
  const [dropIndex, setDropIndex] = useState(null);
  // Mirrors dragIndex for the drop handler: onDrop can fire in the same tick as
  // onDragStart's state update, so reading state there can see a stale null.
  const dragRef = useRef(null);

  // Replaces the whole list and clears the history. For loading a project,
  // which opens a different show rather than editing this one.
  const setScenes = useCallback((next) => {
    reset((prev) => ({
      ...prev,
      scenes: typeof next === 'function' ? next(prev.scenes) : next,
    }));
  }, [reset]);

  const nameOf = (id) => scenes.find((s) => s.id === id)?.name;

  const addScene = useCallback((patch) => execute(sceneCommands.add(patch)), [execute]);
  const patchScene = useCallback((id, patch) => execute(sceneCommands.patch(id, patch)), [execute]);
  const duplicateScene = (id) => execute(sceneCommands.duplicate(id, nameOf(id)));
  const deleteScene = (id) => execute(sceneCommands.delete(id, nameOf(id)));
  const moveScene = useCallback((index, dir) => execute(sceneCommands.move(index, dir)), [execute]);
  const reorder = useCallback((from, to) => execute(sceneCommands.reorder(from, to)), [execute]);

  const onDragStart = useCallback((i) => { dragRef.current = i; setDragIndex(i); }, []);
  const onDragOver = useCallback((i) => setDropIndex(i), []);
  const onDrop = useCallback((i) => {
    reorder(dragRef.current, i);
    dragRef.current = null;
    setDragIndex(null);
    setDropIndex(null);
  }, [reorder]);
  const onDragEnd = useCallback(() => {
    dragRef.current = null;
    setDragIndex(null);
    setDropIndex(null);
  }, []);

  return {
    scenes, setScenes,
    addScene, patchScene, duplicateScene, deleteScene, moveScene, reorder,
    dragIndex, dropIndex, onDragStart, onDragOver, onDrop, onDragEnd,
  };
}
