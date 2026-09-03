import { useState, useCallback, useRef } from 'react';
import { makeScene } from '../engine/sequencer.js';

/**
 * The scene keyframe list and its editing operations: add, patch, duplicate,
 * delete, reorder (buttons) and drag-and-drop reorder.
 */
export default function useSceneList(initial) {
  const [scenes, setScenes] = useState(initial);
  const [dragIndex, setDragIndex] = useState(null);
  const [dropIndex, setDropIndex] = useState(null);
  // Mirrors dragIndex for the drop handler: onDrop can fire in the same tick as
  // onDragStart's state update, so reading state there can see a stale null.
  const dragRef = useRef(null);

  const addScene = useCallback((patch) => {
    setScenes((prev) => [
      ...prev,
      makeScene({ name: `Scene ${prev.length + 1}`, ...patch }),
    ]);
  }, []);

  const patchScene = useCallback((id, patch) => {
    setScenes((prev) => prev.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  }, []);

  const duplicateScene = useCallback((id) => {
    setScenes((prev) => {
      const i = prev.findIndex((s) => s.id === id);
      if (i < 0) return prev;
      // Insert directly after the original — a duplicate appended to the end
      // would need dragging back into place, which is the common case.
      // Drop the source's id before spreading: keeping it would give both cards
      // the same key, so patching or deleting one would hit both.
      const { id: _discarded, ...rest } = prev[i];
      const copy = makeScene({ ...rest, name: `${prev[i].name} copy` });
      return [...prev.slice(0, i + 1), copy, ...prev.slice(i + 1)];
    });
  }, []);

  const deleteScene = useCallback((id) => {
    // A show needs at least one scene; the UI disables the button too, but
    // enforce it here so no caller can empty the list.
    setScenes((prev) => (prev.length <= 1 ? prev : prev.filter((s) => s.id !== id)));
  }, []);

  const moveScene = useCallback((index, dir) => {
    setScenes((prev) => {
      const j = index + dir;
      if (j < 0 || j >= prev.length) return prev;
      const copy = [...prev];
      [copy[index], copy[j]] = [copy[j], copy[index]];
      return copy;
    });
  }, []);

  const reorder = useCallback((from, to) => {
    setScenes((prev) => {
      if (from === to || from == null || to == null) return prev;
      if (from < 0 || from >= prev.length || to < 0 || to >= prev.length) return prev;
      const copy = [...prev];
      const [moved] = copy.splice(from, 1);
      copy.splice(to, 0, moved);
      return copy;
    });
  }, []);

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
