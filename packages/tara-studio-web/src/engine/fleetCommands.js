// Fleet commands (issue #102), applied through the same undo/redo history as
// scene edits (#100). A bulk edit is one command and so one undo step, however
// many vehicles it touches.

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

/** Fields a fleet edit may set, with how to sanitise each. A bad value is dropped. */
const EDITABLE = {
  name: (v) => (typeof v === 'string' ? v : undefined),
  spawnOffset: (v) => (Number.isFinite(v) ? v : undefined),
  hoverAlt: (v) => (Number.isFinite(v) ? Math.max(0, v) : undefined),
  colorHex: (v) => (typeof v === 'string' && /^#[0-9a-f]{6}$/i.test(v) ? v.toLowerCase() : undefined),
};

const FIELD_LABELS = { name: 'call sign', spawnOffset: 'spawn Y', hoverAlt: 'hover altitude', colorHex: 'LED colour' };

const cleanPatch = (patch) => {
  const out = {};
  for (const [k, v] of Object.entries(patch || {})) {
    const clean = EDITABLE[k]?.(v);
    if (clean !== undefined) out[k] = clean;
  }
  return out;
};

const fieldsLabel = (patch) => Object.keys(patch).map((k) => FIELD_LABELS[k] ?? k).join(', ');
const plural = (n) => `${n} vehicle${n === 1 ? '' : 's'}`;

export const fleetCommands = {
  add: () => ({ type: 'fleet/add', label: 'Add vehicle' }),

  remove: (id) => ({ type: 'fleet/remove', label: 'Remove vehicle', id }),

  /** One vehicle, one field: consecutive edits of it merge (typing a call sign). */
  update: (id, field, value) => ({
    type: 'fleet/update',
    label: `Edit ${FIELD_LABELS[field] ?? field}`,
    mergeKey: `fleet/update:${id}:${field}`,
    ids: [id],
    patch: { [field]: value },
  }),

  /** The same patch on every listed vehicle, as one undo step. */
  bulkUpdate: (ids, patch) => ({
    type: 'fleet/update',
    label: `Set ${fieldsLabel(patch)} for ${plural(ids.length)}`,
    ids: [...ids],
    patch: { ...patch },
  }),

  setCount: (count) => ({ type: 'fleet/set-count', label: `Set fleet size to ${count}`, count }),
};

const lowestFreeId = (drones) => {
  const used = new Set(drones.map((d) => d.id));
  let id = 0;
  while (used.has(id)) id++;
  return id;
};

/** Applies a fleet/* command; returns the same array when nothing changes. */
export const applyFleetCommand = (drones, command, maxFleetSize = Infinity) => {
  switch (command.type) {
    case 'fleet/add': {
      if (drones.length >= maxFleetSize) return drones;
      // Reuse the lowest free id rather than max+1, so removing a middle drone
      // and adding one back doesn't leave a gap the show builder would index past.
      return [...drones, makeDrone(lowestFreeId(drones))].sort((a, b) => a.id - b.id);
    }

    case 'fleet/remove': {
      if (drones.length <= 1 || !drones.some((d) => d.id === command.id)) return drones;
      return drones.filter((d) => d.id !== command.id);
    }

    case 'fleet/update': {
      const patch = cleanPatch(command.patch);
      if (!Object.keys(patch).length) return drones;
      const ids = new Set(command.ids);
      let changed = false;
      const next = drones.map((d) => {
        if (!ids.has(d.id) || Object.entries(patch).every(([k, v]) => Object.is(d[k], v))) return d;
        changed = true;
        return { ...d, ...patch };
      });
      return changed ? next : drones;
    }

    case 'fleet/set-count': {
      const n = Math.max(1, Math.min(maxFleetSize, Math.round(command.count)));
      if (!Number.isFinite(n) || n === drones.length) return drones;
      if (n < drones.length) return drones.slice(0, n);
      const next = [...drones];
      while (next.length < n) next.push(makeDrone(lowestFreeId(next)));
      return next.sort((a, b) => a.id - b.id);
    }

    default:
      return drones;
  }
};
