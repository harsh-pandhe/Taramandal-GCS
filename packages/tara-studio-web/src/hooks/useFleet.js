import { useState, useCallback } from 'react';

const PALETTE = [
  '#38bdf8', '#10b981', '#f59e0b', '#ec4899', '#8b5cf6',
  '#3b82f6', '#f43f5e', '#22c55e', '#e11d48', '#2563eb',
];

export const makeDrone = (id) => ({
  id,
  name: `Drone ${String(id).padStart(2, '0')}`,
  hoverAlt: parseFloat((2.0 + id * 0.7).toFixed(1)),
  spawnOffset: parseFloat((2.0 * id).toFixed(1)),
  colorHex: PALETTE[id % PALETTE.length],
});

export const defaultFleet = (n = 3) => Array.from({ length: n }, (_, i) => makeDrone(i));

/** The vehicle list and its editing operations. */
export default function useFleet(initial = defaultFleet()) {
  const [dronesList, setDronesList] = useState(initial);

  const addDrone = useCallback(() => {
    setDronesList((prev) => {
      // Reuse the lowest free id rather than max+1, so removing a middle drone
      // and adding one back doesn't leave a gap the show builder would index past.
      const used = new Set(prev.map((d) => d.id));
      let id = 0;
      while (used.has(id)) id++;
      return [...prev, makeDrone(id)].sort((a, b) => a.id - b.id);
    });
  }, []);

  const removeDrone = useCallback((id) => {
    setDronesList((prev) => (prev.length <= 1 ? prev : prev.filter((d) => d.id !== id)));
  }, []);

  const updateDrone = useCallback((id, field, value) => {
    setDronesList((prev) => prev.map((d) => (d.id === id ? { ...d, [field]: value } : d)));
  }, []);

  const setCount = useCallback((target) => {
    setDronesList((prev) => {
      const n = Math.max(1, target);
      if (n === prev.length) return prev;
      if (n < prev.length) return prev.slice(0, n);
      const next = [...prev];
      for (let i = prev.length; i < n; i++) next.push(makeDrone(i));
      return next;
    });
  }, []);

  return { dronesList, setDronesList, addDrone, removeDrone, updateDrone, setCount };
}
