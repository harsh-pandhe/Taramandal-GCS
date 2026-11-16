// Builds the body of one audit log entry (issues #94, #89). Pure: no React, no
// fetch, no clock. The backend is tara_studio/audit_log.py (AuditEntryIn); this
// file mirrors its rules so an entry the operator's decision produced is not
// refused for size or content. auditEntry.test.js reads audit_log.py and fails if
// a limit here stops matching it.
//
// What the backend refuses, and what is done about it here:
//   * a string over its length (counted in code points)   -> cut by code points
//   * a control character other than \n and \t, or a lone
//     surrogate                                            -> replaced / removed
//   * a serialized entry of 4096 bytes or more, measured
//     as json.dumps(..., ensure_ascii=True) of the model
//     (a BMP non-ASCII character is 6 bytes, an emoji 12) -> shrunk (see buildEntry)
//
// Nothing here ever carries a scene id or a scene name: ops are types, original
// 1-based positions and field names only.

export const LIMITS = Object.freeze({
  request: 200,
  summary: 200,
  reason: 300,
  checkSummary: 200,
  ops: 20,
  fieldsPerOp: 10,
  fieldName: 40,
  /** The backend refuses an entry whose canonical form is >= this many bytes. */
  entryBytes: 4096,
  /** Kept free below that, so a small disagreement in measuring is not a refusal. */
  margin: 128,
});

export const DECISIONS = Object.freeze([
  'applied', 'dismissed', 'stale', 'blocked', 'superseded', 'refused',
  'planner_unavailable', 'planner_timeout', 'planner_failed',
]);
export const SOURCES = Object.freeze(['typed', 'planner']);
export const CHECK_STATUSES = Object.freeze(['PASSED', 'FAILED', 'REFUSED', 'UNVERIFIED']);
export const CHECK_KINDS = Object.freeze(['none', 'open', 'failed', 'unverified']);

const REQUEST_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const RESERVED_REQUEST_ID = 'log_full';

// ------------------------------------------------------------------ request id

const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');

/** A fresh id for one request: matches the backend's ^[A-Za-z0-9_-]{1,64}$. */
export function makeRequestId() {
  const c = globalThis.crypto;
  try {
    if (typeof c?.randomUUID === 'function') return c.randomUUID();
  } catch { /* an insecure context can throw or lack it: fall through */ }
  try {
    if (typeof c?.getRandomValues === 'function') return hex(c.getRandomValues(new Uint8Array(16)));
  } catch { /* fall through */ }
  let id = '';
  for (let i = 0; i < 32; i += 1) id += Math.floor(Math.random() * 16).toString(16);
  return id;
}

// ------------------------------------------------------------------ text

const isLoneSurrogate = (cp) => cp >= 0xd800 && cp <= 0xdfff;
// Unicode category Cc: U+0000-001F and U+007F-009F. \n and \t are the two the backend allows.
const isBadControl = (cp) => (cp <= 0x1f || (cp >= 0x7f && cp <= 0x9f)) && cp !== 0x0a && cp !== 0x09;

/**
 * `value` as text the backend accepts: control characters other than \n and \t
 * become a space, lone surrogates are removed. Never cuts.
 */
export function cleanText(value) {
  if (value === null || value === undefined) return '';
  let out = '';
  for (const ch of String(value)) {       // iterates code points; a lone surrogate comes out alone
    const cp = ch.codePointAt(0);
    if (isLoneSurrogate(cp)) continue;
    out += isBadControl(cp) ? ' ' : ch;
  }
  return out;
}

/** The first `max` code points of `value`, with a single "…" as the last of them when it was cut. */
export function cutText(value, max) {
  const cps = [...value];
  if (cps.length <= max) return value;
  if (max <= 0) return '';
  return `${cps.slice(0, max - 1).join('')}…`;
}

/** cleanText then cutText: what a free-text field may hold. */
export const fitText = (value, max) => cutText(cleanText(value), max);

// ------------------------------------------------------------------ size

// json.dumps(ensure_ascii=True) of one string, as Python writes it. Each UTF-16
// unit outside space..~ is \uXXXX (so an astral character is two escapes, 12 bytes).
const SHORT_ESCAPES = { '"': '\\"', '\\': '\\\\', '\n': '\\n', '\r': '\\r', '\t': '\\t', '\b': '\\b', '\f': '\\f' };
function pyString(s) {
  let out = '"';
  for (let i = 0; i < s.length; i += 1) {
    const ch = s[i];
    const code = s.charCodeAt(i);
    if (SHORT_ESCAPES[ch]) out += SHORT_ESCAPES[ch];
    else if (code >= 0x20 && code <= 0x7e) out += ch;
    else out += `\\u${code.toString(16).padStart(4, '0')}`;
  }
  return `${out}"`;
}

// json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True) of plain JSON data.
function pyCanonical(v) {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'string') return pyString(v);
  if (typeof v === 'number') return String(v);
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (Array.isArray(v)) return `[${v.map(pyCanonical).join(',')}]`;
  return `{${Object.keys(v).sort().map((k) => `${pyString(k)}:${pyCanonical(v[k])}`).join(',')}}`;
}

/**
 * The entry as the backend's model holds it after validation: `model_dump()` of
 * AuditEntryIn fills every optional field (sceneIndex, fields, check, kind,
 * reason) with its default, so those count even when the request left them out.
 */
function asModelDump(entry) {
  return {
    v: entry.v,
    requestId: entry.requestId,
    source: entry.source,
    request: entry.request,
    summary: entry.summary,
    ops: (entry.ops ?? []).map((op) => ({ type: op.type, sceneIndex: op.sceneIndex ?? null, fields: op.fields ?? [] })),
    check: entry.check ? { status: entry.check.status, kind: entry.check.kind ?? null, summary: entry.check.summary } : null,
    decision: entry.decision,
    reason: entry.reason ?? null,
  };
}

/**
 * The size the backend measures in AuditEntryIn._checked:
 * len(_canonical(model_dump())), where _canonical is json.dumps with sorted keys,
 * compact separators and ensure_ascii=True, encoded as ASCII. The backend refuses
 * an entry when that is >= 4096.
 */
export const canonicalSize = (entry) => pyCanonical(asModelDump(entry)).length;

// ------------------------------------------------------------------ ops

const cleanFieldNames = (keys) => {
  const out = [];
  for (const key of keys) {
    if (typeof key !== 'string') continue;
    const name = fitText(key, LIMITS.fieldName).trim();
    if (name) out.push(name);
    if (out.length >= LIMITS.fieldsPerOp) break;
  }
  return out;
};

/**
 * The audit `ops` of a scene command: [{ type, sceneIndex?, fields? }].
 *
 * `sceneIndex` is the scene's 1-based position in the scene list the batch was
 * built from (`expectIds`), never an id or a name. A batch's reorder/move
 * indices are positions in the running order at that step, so that order is
 * replayed (add pushes, delete splices, duplicate inserts after the original,
 * reorder splices from -> to), the way planCompile.js builds them; a scene the
 * batch itself created has no original position and gets no sceneIndex.
 * A command that is not a batch (no expectIds) gets no sceneIndex either.
 */
export function auditOps(command) {
  if (!command || typeof command !== 'object') return [];
  const batch = command.type === 'scene/batch';
  const commands = batch ? command.commands : [command];
  if (!Array.isArray(commands)) return [];

  const original = batch && Array.isArray(command.expectIds) ? command.expectIds : [];
  const order = [...original];
  const position = (id) => {
    const at = original.indexOf(id);
    return at >= 0 ? at + 1 : undefined;
  };
  const ops = [];
  const push = (type, sceneIndex, fields) => {
    const op = { type };
    if (Number.isInteger(sceneIndex) && sceneIndex >= 1) op.sceneIndex = sceneIndex;
    if (fields && fields.length) op.fields = fields;
    ops.push(op);
  };

  for (const c of commands) {
    if (!c || typeof c !== 'object') { push('other'); continue; }
    switch (c.type) {
      case 'scene/patch':
        push('patch', position(c.id), cleanFieldNames(c.patch && typeof c.patch === 'object' ? Object.keys(c.patch) : []));
        break;
      case 'scene/add':
        if (c.scene?.id !== undefined) order.push(c.scene.id);
        push('add');
        break;
      case 'scene/delete': {
        push('delete', position(c.id));
        const at = order.indexOf(c.id);
        if (at >= 0) order.splice(at, 1);
        break;
      }
      case 'scene/duplicate': {
        push('duplicate', position(c.id));
        const at = order.indexOf(c.id);
        if (at >= 0) order.splice(at + 1, 0, c.copyId);
        break;
      }
      case 'scene/reorder': {
        const { from, to } = c;
        const ok = Number.isInteger(from) && Number.isInteger(to) && from >= 0 && to >= 0 && from < order.length && to < order.length;
        push('move', ok ? position(order[from]) : undefined);
        if (ok) { const [moved] = order.splice(from, 1); order.splice(to, 0, moved); }
        break;
      }
      case 'scene/move': {
        const { index, dir } = c;
        const j = index + dir;
        const ok = Number.isInteger(index) && Number.isInteger(dir) && index >= 0 && index < order.length && j >= 0 && j < order.length;
        push('move', ok ? position(order[index]) : undefined);
        if (ok) [order[index], order[j]] = [order[j], order[index]];
        break;
      }
      default:
        push('other');
    }
  }
  return ops.slice(0, LIMITS.ops);
}

// ------------------------------------------------------------------ entry

/**
 * The exact body to POST to /api/projects/{id}/audit. Never carries seq, at,
 * projectId, prev or hash: the server owns those.
 *
 * `check` is the result of checkProposal ({ status, kind, summary }). A check
 * that is not a final answer (status CHECKING, or anything the backend has no
 * word for) is left out: nothing was concluded.
 *
 * Throws only for a programmer error: an unknown decision or source, or a
 * requestId the backend would refuse. The caller should wrap the call anyway.
 * Over-long or odd TEXT is never an error: it is cleaned and cut, and when the
 * entry is still too big for the backend's byte limit it shrinks, in this order,
 * until it is under (4096 - 128) bytes: reason, then summary, then check.summary,
 * then the ops (field names cut from the end, then whole ops from the end, and
 * "; ops shortened" added to the reason if there is room for it), and the operator's
 * request text last. Text is cut by code points (a "…" ends a cut one) and, only when
 * the optional `reason` is emptied, omitted. Size never throws.
 */
export function buildEntry({ requestId, source, request, summary, command, check, decision, reason }) {
  if (!SOURCES.includes(source)) throw new Error(`audit entry: unknown source ${JSON.stringify(source)}`);
  if (!DECISIONS.includes(decision)) throw new Error(`audit entry: unknown decision ${JSON.stringify(decision)}`);
  if (typeof requestId !== 'string' || !REQUEST_ID_RE.test(requestId) || requestId === RESERVED_REQUEST_ID) {
    throw new Error('audit entry: requestId must be 1-64 of A-Z a-z 0-9 _ - and not "log_full"');
  }

  const entry = {
    v: 1,
    requestId,
    source,
    request: fitText(request, LIMITS.request),
    summary: fitText(summary, LIMITS.summary),
    ops: auditOps(command),
    decision,
  };
  if (check && CHECK_STATUSES.includes(check.status)) {
    entry.check = {
      status: check.status,
      kind: CHECK_KINDS.includes(check.kind) ? check.kind : null,
      summary: fitText(check.summary, LIMITS.checkSummary),
    };
  }
  const why = fitText(reason, LIMITS.reason);
  if (why) entry.reason = why;

  const budget = LIMITS.entryBytes - LIMITS.margin;
  // The text fields in the order they give way. `get`/`set` keep the optional ones optional.
  const reasonField = { get: () => entry.reason ?? '', set: (t) => { if (t) entry.reason = t; else delete entry.reason; } };
  const summaryField = { get: () => entry.summary, set: (t) => { entry.summary = t; } };
  const checkField = { get: () => entry.check?.summary ?? '', set: (t) => { if (entry.check) entry.check.summary = t; } };
  const requestField = { get: () => entry.request, set: (t) => { entry.request = t; } };
  const shrink = (field) => {
    if (canonicalSize(entry) <= budget) return;
    const full = field.get();
    const total = [...full].length;
    for (let keep = total - 1; keep >= 0 && canonicalSize(entry) > budget; keep -= 1) {
      field.set(keep === 0 ? '' : cutText(full, keep));
    }
  };

  shrink(reasonField);
  shrink(summaryField);
  shrink(checkField);

  // Then the ops: first fewer field names per op (cut from the end), then whole ops from
  // the end. What is kept is kept as it was: nothing is invented and the order stays. The
  // operator's own request text is the last thing to give way, after the ops.
  if (canonicalSize(entry) > budget) {
    const whole = entry.ops;
    const fieldsOf = (op) => op.fields ?? [];
    const widest = Math.max(0, ...whole.map((op) => fieldsOf(op).length));
    const withFields = (k) => whole.map((op) => {
      const { fields: _all, ...rest } = op;
      return fieldsOf(op).length && k > 0 ? { ...rest, fields: fieldsOf(op).slice(0, k) } : rest;
    });
    for (let k = widest - 1; k >= 0 && canonicalSize(entry) > budget; k -= 1) entry.ops = withFields(k);
    while (canonicalSize(entry) > budget && entry.ops.length > 0) entry.ops = entry.ops.slice(0, -1);
    const note = entry.reason ? `${entry.reason}; ops shortened` : 'ops shortened';
    const noted = { ...entry, reason: note };
    if ([...note].length <= LIMITS.reason && canonicalSize(noted) <= budget) entry.reason = note;
  }
  shrink(requestField);
  // Only reachable if even an entry with no ops and empty text is too big: it cannot be.
  if (canonicalSize(entry) > budget) throw new Error('audit entry: cannot be made small enough for the audit log');
  return entry;
}
