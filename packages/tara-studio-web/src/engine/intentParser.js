// Fixed-grammar intent parser (issue #89). Turns one typed sentence into a
// pinned, validated scene/batch command - or refuses with a reason. There is no
// model and no guessing: a sentence either matches one grammar line exactly or
// is refused. The result is only ever a command for the undo history; nothing
// here applies it.
//
// Grammar (case-insensitive, one action per input, scene numbers are 1-based):
//   make scene N a <shape>          set hold of scene N to X s
//   set transition of scene N to X s
//   move scene N after|before scene M
//   delete scene N                  duplicate scene N
//   add scene <shape>

import { SHAPE_LIBRARY } from './formations.js';
import { sceneCommands, batchRefusal } from './commands.js';
import { sceneSignature } from './sceneValidation.js';

const fail = (reason) => ({ ok: false, reason });

// Anything about limits or about acting on vehicles is out of scope. These are
// refused up front, with a distinct reason, before the grammar is consulted.
const OUT_OF_SCOPE = /\b(limits?|transmit\w*|export\w*|upload\w*|gcs|start\w*|arm\w*|land\w*|send\w*|sign\w*|takeoff|launch\w*|altitude|speed|separation|geofence)\b/;

const MULTI_ACTION = /\b(and|then|also)\b|[,;&]/;

// Shape names accepted in sentences: each library label, plus the payload name
// of parametric shapes ("circle" for the ring, "vee" for the V-formation).
const SHAPE_BY_NAME = (() => {
  const map = new Map();
  for (const entry of SHAPE_LIBRARY) {
    const names = [entry.label.toLowerCase()];
    if (entry.shapeType === 'parametric') names.push(entry.shapePayload.toLowerCase());
    for (const n of names) map.set(n, entry);
  }
  return map;
})();

const sceneIndex = (token, doc) => {
  if (!/^[1-9]\d*$/.test(token)) return { error: `"${token}" is not a scene number (scenes are numbered from 1)` };
  const n = Number(token);
  if (n > doc.scenes.length) {
    return { error: `scene ${n} does not exist (the show has ${doc.scenes.length})` };
  }
  return { index: n - 1 };
};

const shapeEntry = (name) => SHAPE_BY_NAME.get(name.trim());

const sceneName = (doc, i) => `"${doc.scenes[i].name}"`;

export const parseIntent = (text, doc) => {
  if (!doc || !Array.isArray(doc.scenes) || doc.scenes.length === 0) return fail('there is no show to edit');
  if (typeof text !== 'string') return fail('empty request');
  const input = text.trim().toLowerCase().replace(/\s+/g, ' ').replace(/\.$/, '');
  if (!input) return fail('empty request');
  if (OUT_OF_SCOPE.test(input)) return fail('not supported: this assistant only edits scene shapes, timing and order');
  if (MULTI_ACTION.test(input)) return fail('one action per request, please');

  const ids = doc.scenes.map((s) => s.id);
  const build = (commands, summary) => {
    const command = sceneCommands.batch(commands, ids, sceneSignature(doc), summary);
    const reason = batchRefusal(doc, command);
    return reason ? fail(reason) : { ok: true, command, summary };
  };

  let m;

  if ((m = input.match(/^make scene (\S+) an? (.+)$/))) {
    const s = sceneIndex(m[1], doc);
    if (s.error) return fail(s.error);
    const shape = shapeEntry(m[2]);
    if (!shape) return fail(`unknown shape "${m[2]}"`);
    return build(
      [sceneCommands.patch(ids[s.index], { shapeType: shape.shapeType, shapePayload: shape.shapePayload })],
      `Make scene ${m[1]} (${sceneName(doc, s.index)}) a ${shape.label}`,
    );
  }

  if ((m = input.match(/^set (hold|transition) of scene (\S+) to (-?\d+(?:\.\d+)?) ?(?:s|sec|secs|second|seconds)$/))) {
    const s = sceneIndex(m[2], doc);
    if (s.error) return fail(s.error);
    const field = m[1] === 'hold' ? 'holdDurationSeconds' : 'transitionDurationSeconds';
    const value = Number(m[3]);
    return build(
      [sceneCommands.patch(ids[s.index], { [field]: value })],
      `Set ${m[1]} of scene ${m[2]} (${sceneName(doc, s.index)}) to ${value} s`,
    );
  }

  if ((m = input.match(/^move scene (\S+) (after|before) scene (\S+)$/))) {
    const a = sceneIndex(m[1], doc);
    if (a.error) return fail(a.error);
    const b = sceneIndex(m[3], doc);
    if (b.error) return fail(b.error);
    if (a.index === b.index) return fail('a scene cannot be moved relative to itself');
    // reorder(from, to) removes the scene then inserts it at `to`.
    const from = a.index;
    const anchor = b.index;
    const to = m[2] === 'after'
      ? (from < anchor ? anchor : anchor + 1)
      : (from < anchor ? anchor - 1 : anchor);
    return build(
      [sceneCommands.reorder(from, to)],
      `Move scene ${m[1]} (${sceneName(doc, from)}) ${m[2]} scene ${m[3]}`,
    );
  }

  if ((m = input.match(/^delete scene (\S+)$/))) {
    const s = sceneIndex(m[1], doc);
    if (s.error) return fail(s.error);
    return build(
      [sceneCommands.delete(ids[s.index], doc.scenes[s.index].name)],
      `Delete scene ${m[1]} (${sceneName(doc, s.index)})`,
    );
  }

  if ((m = input.match(/^duplicate scene (\S+)$/))) {
    const s = sceneIndex(m[1], doc);
    if (s.error) return fail(s.error);
    return build(
      [sceneCommands.duplicate(ids[s.index], doc.scenes[s.index].name)],
      `Duplicate scene ${m[1]} (${sceneName(doc, s.index)})`,
    );
  }

  if ((m = input.match(/^add scene (.+)$/))) {
    const shape = shapeEntry(m[1]);
    if (!shape) return fail(`unknown shape "${m[1]}"`);
    return build(
      [sceneCommands.add({ shapeType: shape.shapeType, shapePayload: shape.shapePayload })],
      `Add a ${shape.label} scene`,
    );
  }

  return fail('not understood: try "make scene 2 a heart", "set hold of scene 1 to 8 s", "move scene 3 after scene 1", "delete scene 2", "duplicate scene 1" or "add scene sphere"');
};
