// What the AI decision log panel shows (issue #94), as plain data. Pure apart from
// loadAuditView, which only calls the client it is given. Every string the panel
// prints comes from here, so the wording is unit-tested and the component stays thin.
//
// Entries come from a file on disk that a person could have edited, so nothing in an
// entry is trusted to have its type: every field is read through `text`, and an unknown
// decision or check still renders.

import { CHECK_KINDS } from './auditEntry.js';

export const LIST_LIMIT = 20;

/** The decision words for every `decision` the backend accepts, plus its own `log_full` marker. */
export const DECISION_LABELS = Object.freeze({
  applied: 'Applied',
  dismissed: 'Dismissed',
  stale: 'Not applied: scenes changed',
  blocked: 'Blocked',
  superseded: 'Replaced by a newer request',
  refused: 'Refused',
  planner_unavailable: 'Planner unavailable',
  planner_timeout: 'Planner timed out',
  planner_failed: 'Planner failed',
  log_full: 'Log full',
});

export const SOURCE_LABELS = Object.freeze({ typed: 'Typed', planner: 'Planner' });

/**
 * One short phrase per exportGate `kind` (engine/exportGate.js): what that result means for
 * exporting the edited show. The verdict is the kind, never the validator's raw status.
 */
export const KIND_PHRASES = Object.freeze({
  open: 'Safety check passed: export stays open',
  failed: 'Safety check failed: export would be locked',
  unverified: 'Safety check not verified',
  none: 'No show to check',
});

// Only when there is no kind (the build was refused, or an older entry): the status as a label.
const STATUS_FALLBACK = Object.freeze({
  REFUSED: 'Safety check could not run: the edited show was refused',
  PASSED: 'Safety check: passed',
  FAILED: 'Safety check: failed',
  UNVERIFIED: 'Safety check not verified',
});

export const MESSAGES = Object.freeze({
  noProject: 'No saved project yet, so nothing is recorded.',
  loading: 'Loading the decision log…',
  empty: 'No AI decisions recorded for this project yet.',
  error: 'Could not read the decision log.',
  integrity: 'This log failed its integrity check: it may have been edited or damaged.',
  full: 'The log is full: new decisions are not recorded.',
});

const text = (v) => (typeof v === 'string' ? v : '');
const hasOwn = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);

/** The decision in plain words. An unknown (future) value is shown as it is, cut short. */
export const decisionLabel = (decision) => {
  const d = text(decision);
  if (hasOwn(DECISION_LABELS, d)) return DECISION_LABELS[d];
  return d ? `Other: ${[...d].slice(0, 30).join('')}` : 'Other decision';
};

export const sourceLabel = (source) => {
  const s = text(source);
  return hasOwn(SOURCE_LABELS, s) ? SOURCE_LABELS[s] : '';
};

/** The check as one phrase, from `kind`; the status is only a fallback when there is no kind. */
export const checkPhrase = (check) => {
  if (!check || typeof check !== 'object') return null;
  if (typeof check.kind === 'string' && CHECK_KINDS.includes(check.kind)) return KIND_PHRASES[check.kind];
  const status = text(check.status);
  return hasOwn(STATUS_FALLBACK, status) ? STATUS_FALLBACK[status] : null;
};

/** `at` as a local, readable date and time. An unreadable stamp is shown as it is. */
export const formatWhen = (at, { locale, timeZone } = {}) => {
  const raw = text(at);
  const date = new Date(raw);
  if (!raw || Number.isNaN(date.getTime())) return raw;
  try {
    return date.toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'medium', ...(timeZone ? { timeZone } : {}) });
  } catch {
    return raw;
  }
};

/** One list row: everything the panel prints for an entry, as text. */
export const entryRow = (entry, index = 0, options = {}) => {
  const e = entry && typeof entry === 'object' ? entry : {};
  return {
    key: Number.isInteger(e.seq) ? `seq-${e.seq}` : `row-${index}`,
    when: formatWhen(e.at, options),
    decision: decisionLabel(e.decision),
    source: sourceLabel(e.source),
    request: text(e.request),
    summary: text(e.summary),
    reason: text(e.reason),
    check: checkPhrase(e.check),
  };
};

/**
 * What to tell the operator about the log itself: a failed integrity check (do not say
 * more than that: a damaged file and an edited one look the same) and a full log.
 */
export const logNotices = (read) => {
  const notices = [];
  if (read.chainOk === false || read.torn === true || (Number.isInteger(read.corruptLines) && read.corruptLines > 0)) {
    notices.push(MESSAGES.integrity);
  }
  if (read.full === true) notices.push(MESSAGES.full);
  return notices;
};

/**
 * Reads the newest entries of a project's log into a view state:
 *   { kind: 'noproject' } | { kind: 'error' } | { kind: 'empty', notices } |
 *   { kind: 'ready', rows, notices, total }
 * Never throws. `client` is an auditClient.
 */
export async function loadAuditView(client, projectId, limit = LIST_LIMIT, options = {}) {
  if (typeof projectId !== 'string' || !projectId) return { kind: 'noproject' };
  let read;
  try {
    read = await client.list(projectId, limit);
  } catch {
    return { kind: 'error' };
  }
  if (!read?.ok || !Array.isArray(read.entries)) return { kind: 'error' };
  const notices = logNotices(read);
  if (read.entries.length === 0) return { kind: 'empty', notices };
  return {
    kind: 'ready',
    notices,
    total: Number.isInteger(read.total) ? read.total : read.entries.length,
    rows: read.entries.map((e, i) => entryRow(e, i, options)),
  };
}
