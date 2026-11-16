import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import {
  DECISION_LABELS, KIND_PHRASES, MESSAGES, checkPhrase, decisionLabel, entryRow, formatWhen, loadAuditView, logNotices, sourceLabel,
} from './auditView.js';
import { DECISIONS, CHECK_KINDS } from './auditEntry.js';
import { exportGate } from './exportGate.js';

const AT = '2026-11-16T09:05:07.000Z';
const entry = (over = {}) => ({
  v: 1, seq: 3, at: AT, projectId: 'p', requestId: 'r', source: 'typed', request: 'set hold of scene 1 to 8 s',
  summary: 'Edit scene 1: hold 8 s', ops: [], check: { status: 'PASSED', kind: 'open', summary: 'ok' }, decision: 'applied', reason: null,
  ...over,
});

describe('decision wording', () => {
  it.each([
    ['applied', 'Applied'], ['dismissed', 'Dismissed'], ['stale', 'Not applied: scenes changed'], ['blocked', 'Blocked'],
    ['superseded', 'Replaced by a newer request'], ['refused', 'Refused'], ['planner_unavailable', 'Planner unavailable'],
    ['planner_timeout', 'Planner timed out'], ['planner_failed', 'Planner failed'], ['log_full', 'Log full'],
  ])('%s -> %s', (decision, label) => {
    expect(decisionLabel(decision)).toBe(label);
  });

  it('every decision the backend accepts, and its own log_full marker, has a label', () => {
    for (const d of [...DECISIONS, 'log_full']) expect(DECISION_LABELS).toHaveProperty(d);
  });

  it('reads the decisions the backend accepts from audit_log.py, so a new one cannot go unlabelled', () => {
    const py = readFileSync(new URL('../../../tara-studio/tara_studio/audit_log.py', import.meta.url), 'utf8');
    const all = [...py.match(/decision: Literal\[([\s\S]*?)\]/)[1].matchAll(/"(\w+)"/g)].map((m) => m[1]);
    for (const d of [...all, py.match(/FULL_DECISION = "(\w+)"/)[1]]) expect(DECISION_LABELS).toHaveProperty(d);
  });

  it('an unknown future value, or none, still renders safely', () => {
    expect(decisionLabel('teleported')).toBe('Other: teleported');
    expect(decisionLabel('<img src=x onerror=alert(1)>')).toBe('Other: <img src=x onerror=alert(1)>');
    expect(decisionLabel('x'.repeat(100))).toBe(`Other: ${'x'.repeat(30)}`);
    expect(decisionLabel('constructor')).toBe('Other: constructor');
    expect(decisionLabel('__proto__')).toBe('Other: __proto__');
    for (const bad of [undefined, null, 5, {}, '']) expect(decisionLabel(bad)).toBe('Other decision');
  });

  it('sources', () => {
    expect(sourceLabel('typed')).toBe('Typed');
    expect(sourceLabel('planner')).toBe('Planner');
    expect(sourceLabel('robot')).toBe('');
    expect(sourceLabel(undefined)).toBe('');
  });
});

describe('the check, from kind through exportGate', () => {
  it('every kind exportGate can return has a phrase, and the phrases differ', () => {
    const kinds = new Set([
      exportGate({ hasShow: false }).kind,
      exportGate({ hasShow: true, validation: { status: 'PASSED' } }).kind,
      exportGate({ hasShow: true, validation: { status: 'FAILED' } }).kind,
      exportGate({ hasShow: true, validation: null }).kind,
    ]);
    expect(kinds).toEqual(new Set(CHECK_KINDS));
    for (const kind of kinds) expect(KIND_PHRASES[kind]).toBeTruthy();
    expect(new Set(Object.values(KIND_PHRASES)).size).toBe(CHECK_KINDS.length);
  });

  it.each(CHECK_KINDS)('kind %s wins over whatever the status says', (kind) => {
    for (const status of ['PASSED', 'FAILED', 'REFUSED', 'UNVERIFIED']) {
      expect(checkPhrase({ status, kind, summary: 's' })).toBe(KIND_PHRASES[kind]);
    }
  });

  it('a PASSED status with kind failed (a show that is not flight-certified) reads as failed', () => {
    expect(checkPhrase({ status: 'FAILED', kind: 'failed', summary: 'jerk' })).toMatch(/failed/);
    expect(checkPhrase({ status: 'PASSED', kind: 'failed', summary: 'x' })).not.toMatch(/passed/);
  });

  it('with no kind the status is a fallback label; REFUSED says the show could not be built', () => {
    expect(checkPhrase({ status: 'REFUSED', kind: null, summary: 's' })).toMatch(/refused/);
    expect(checkPhrase({ status: 'PASSED', kind: null })).toBe('Safety check: passed');
    expect(checkPhrase({ status: 'UNVERIFIED' })).toBe('Safety check not verified');
  });

  it('no check, or one that makes no sense, shows nothing', () => {
    for (const bad of [null, undefined, 'x', 5, {}, { status: 'MAYBE' }, { kind: 'bogus' }]) expect(checkPhrase(bad)).toBeNull();
  });
});

describe('the row', () => {
  it('has every field as text, and the time in the locale and zone asked for', () => {
    const row = entryRow(entry({ decision: 'refused', reason: 'not supported' }), 0, { locale: 'en-GB', timeZone: 'UTC' });
    expect(row).toEqual({
      key: 'seq-3', when: '16 Nov 2026, 09:05:07', decision: 'Refused', source: 'Typed',
      request: 'set hold of scene 1 to 8 s', summary: 'Edit scene 1: hold 8 s', reason: 'not supported',
      check: KIND_PHRASES.open,
    });
  });

  it('a stamp that is not a date is shown as it is; none is empty', () => {
    expect(formatWhen('not a date')).toBe('not a date');
    expect(formatWhen(undefined)).toBe('');
    expect(formatWhen(42)).toBe('');
  });

  it('survives an entry whose fields are the wrong type or missing', () => {
    for (const bad of [null, undefined, 'x', 5, [], {}, { request: 5, summary: {}, reason: [], check: 'x', decision: 7, source: null, at: {} }]) {
      const row = entryRow(bad, 2);
      for (const k of ['when', 'decision', 'source', 'request', 'summary', 'reason']) expect(typeof row[k]).toBe('string');
      expect(row.key).toBe('row-2');
      expect(row.check).toBeNull();
    }
  });

  it('long Devanagari, CJK and emoji text passes through whole, as text (the page wraps it)', () => {
    const long = `${'क'.repeat(200)}${'中'.repeat(100)}${'😀'.repeat(50)}${'x'.repeat(150)}`;
    const row = entryRow(entry({ request: long, summary: long, reason: long }));
    expect(row.request).toBe(long);
    expect(row.summary).toBe(long);
    expect(row.reason).toBe(long);
  });

  it('markup in an entry stays a plain string', () => {
    expect(entryRow(entry({ request: '<script>alert(1)</script>' })).request).toBe('<script>alert(1)</script>');
  });
});

describe('notices about the log itself', () => {
  const ok = { chainOk: true, torn: false, corruptLines: 0, full: false };
  it('none for a healthy log', () => { expect(logNotices(ok)).toEqual([]); });
  it('chainOk false -> the integrity wording, which does not say tampered', () => {
    expect(logNotices({ ...ok, chainOk: false })).toEqual([MESSAGES.integrity]);
    expect(MESSAGES.integrity).toBe('This log failed its integrity check: it may have been edited or damaged.');
    expect(MESSAGES.integrity).not.toMatch(/tamper/i);
  });
  it('torn, or any corrupt line, is the same integrity notice', () => {
    expect(logNotices({ ...ok, torn: true })).toEqual([MESSAGES.integrity]);
    expect(logNotices({ ...ok, corruptLines: 2 })).toEqual([MESSAGES.integrity]);
  });
  it('full -> the full notice, and both together give both', () => {
    expect(logNotices({ ...ok, full: true })).toEqual([MESSAGES.full]);
    expect(MESSAGES.full).toBe('The log is full: new decisions are not recorded.');
    expect(logNotices({ chainOk: false, full: true })).toEqual([MESSAGES.integrity, MESSAGES.full]);
  });
});

describe('loadAuditView', () => {
  const read = (over = {}) => ({ ok: true, entries: [entry({ seq: 2 }), entry({ seq: 1, decision: 'dismissed' })], total: 2, full: false, chainOk: true, torn: false, corruptLines: 0, ...over });
  const clientOf = (impl) => ({ list: vi.fn(impl) });

  it('ok: the rows, newest first as sent, asked for with the limit', async () => {
    const client = clientOf(async () => read());
    const view = await loadAuditView(client, 'proj-1', 20, { locale: 'en-GB', timeZone: 'UTC' });
    expect(client.list).toHaveBeenCalledWith('proj-1', 20);
    expect(view.kind).toBe('ready');
    expect(view.rows.map((r) => r.decision)).toEqual(['Applied', 'Dismissed']);
    expect(view.total).toBe(2);
    expect(view.notices).toEqual([]);
  });

  it('empty', async () => {
    expect(await loadAuditView(clientOf(async () => read({ entries: [], total: 0 })), 'p')).toEqual({ kind: 'empty', notices: [] });
  });

  it('error: a failed read, a thrown one, and an answer that is not a list', async () => {
    expect(await loadAuditView(clientOf(async () => ({ ok: false, code: 'network', status: 0 })), 'p')).toEqual({ kind: 'error' });
    expect(await loadAuditView(clientOf(async () => { throw new Error('x'); }), 'p')).toEqual({ kind: 'error' });
    expect(await loadAuditView(clientOf(async () => ({ ok: true, entries: 'no' })), 'p')).toEqual({ kind: 'error' });
    expect(await loadAuditView(clientOf(async () => null), 'p')).toEqual({ kind: 'error' });
  });

  it('no project: nothing is read', async () => {
    const client = clientOf(async () => read());
    for (const id of [null, undefined, '', 5]) expect(await loadAuditView(client, id)).toEqual({ kind: 'noproject' });
    expect(client.list).not.toHaveBeenCalled();
  });

  it('carries the integrity and full notices, with rows or without', async () => {
    expect((await loadAuditView(clientOf(async () => read({ chainOk: false })), 'p')).notices).toEqual([MESSAGES.integrity]);
    expect(await loadAuditView(clientOf(async () => read({ entries: [], total: 0, full: true })), 'p'))
      .toEqual({ kind: 'empty', notices: [MESSAGES.full] });
  });
});
