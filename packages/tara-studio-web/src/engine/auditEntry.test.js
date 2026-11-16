import { readFileSync } from 'node:fs';
import { describe, expect, it, vi, afterEach } from 'vitest';
import {
  LIMITS, DECISIONS, buildEntry, auditOps, canonicalSize, cleanText, cutText, fitText, makeRequestId,
} from './auditEntry.js';
import { sceneCommands } from './commands.js';
import { exportGate } from './exportGate.js';

const REQUEST_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const SPLIT_PAIR = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;
const base = (over = {}) => ({
  requestId: 'req-1', source: 'typed', request: 'set hold of scene 1 to 8 s', summary: 'Edit scene 1: hold 8 s',
  command: null, check: { status: 'PASSED', kind: 'open', summary: 'Passed all safety checks.' },
  decision: 'applied', reason: undefined, ...over,
});
const FORBIDDEN = ['seq', 'at', 'projectId', 'prev', 'hash'];
const ids = ['a', 'b', 'c', 'd'];
const batch = (commands, expectIds = ids) => sceneCommands.batch(commands, expectIds, 'sig', 'label');
const dev = (n) => 'क'.repeat(n);
const BUDGET = LIMITS.entryBytes - LIMITS.margin;

afterEach(() => vi.unstubAllGlobals());

describe('makeRequestId', () => {
  it('matches the backend regex over 1000 ids, never "log_full", and does not repeat', () => {
    const seen = new Set();
    for (let i = 0; i < 1000; i += 1) {
      const id = makeRequestId();
      expect(id).toMatch(REQUEST_ID_RE);
      expect(id).not.toBe('log_full');
      seen.add(id);
    }
    expect(seen.size).toBe(1000);
  });

  it('falls back to 32 hex characters from getRandomValues', () => {
    vi.stubGlobal('crypto', { getRandomValues: (a) => a.fill(171) });
    expect(makeRequestId()).toBe('ab'.repeat(16));
  });

  it('falls back to Math.random hex when there is no crypto, or it throws', () => {
    vi.stubGlobal('crypto', undefined);
    expect(makeRequestId()).toMatch(/^[0-9a-f]{32}$/);
    vi.stubGlobal('crypto', { randomUUID: () => { throw new Error('insecure'); }, getRandomValues: () => { throw new Error('no'); } });
    for (let i = 0; i < 1000; i += 1) expect(makeRequestId()).toMatch(REQUEST_ID_RE);
  });
});

describe('text cleaning', () => {
  it('keeps \\n and \\t, replaces other control characters with a space', () => {
    expect(cleanText('a\nb\tc')).toBe('a\nb\tc');
    expect(cleanText('a\x00b\x1b[31m\rc\x7fd\x85e')).toBe('a b [31m c d e');
    expect(cleanText(null)).toBe('');
    expect(cleanText(42)).toBe('42');
  });

  it('removes lone surrogates but keeps a real pair', () => {
    expect(cleanText('a\ud800b\udc00c')).toBe('abc');
    expect(cleanText('x😀y')).toBe('x😀y');
  });

  it('cuts by code points, adds one … only when it cuts, and never splits a pair', () => {
    expect(cutText('abc', 3)).toBe('abc');
    expect(cutText('abcd', 3)).toBe('ab…');
    expect(cutText('😀😀😀😀', 3)).toBe('😀😀…');
    for (let n = 1; n <= 6; n += 1) {
      const cut = cutText('a😀b😀c😀d', n);
      expect([...cut].length).toBeLessThanOrEqual(n);
      expect(cut).not.toMatch(SPLIT_PAIR);
    }
    expect(cutText('abc', 0)).toBe('');
    expect(cutText('abc', 1)).toBe('…');
  });

  it('counts 200 emoji as 200, though JS length says 400', () => {
    const out = fitText('😀'.repeat(300), 200);
    expect([...out].length).toBe(200);
    expect(out.endsWith('…')).toBe(true);
  });
});

describe('canonicalSize', () => {
  it('matches json.dumps(sort_keys, compact, ensure_ascii) of the filled-in model for a known entry', () => {
    const e = { v: 1, requestId: 'r', source: 'typed', request: 'क', summary: '😀"\\\n', ops: [], decision: 'applied' };
    const expected = '{"check":null,"decision":"applied","ops":[],"reason":null,"request":"\\u0915","requestId":"r",'
      + '"source":"typed","summary":"\\ud83d\\ude00\\"\\\\\\n","v":1}';
    expect(canonicalSize(e)).toBe(expected.length);
  });

  it('counts the defaults the backend fills in (null sceneIndex, empty fields, null kind)', () => {
    const lean = { v: 1, requestId: 'r', source: 'typed', request: '', summary: '', ops: [{ type: 'add' }], decision: 'applied' };
    const expected = '{"check":null,"decision":"applied","ops":[{"fields":[],"sceneIndex":null,"type":"add"}],'
      + '"reason":null,"request":"","requestId":"r","source":"typed","summary":"","v":1}';
    expect(canonicalSize(lean)).toBe(expected.length);
  });

  it('costs 6 bytes for a BMP character and 12 for an emoji', () => {
    const at = (s) => canonicalSize({ ...base(), request: s, check: undefined });
    expect(at('क') - at('')).toBe(6);
    expect(at('😀') - at('')).toBe(12);
    expect(at('a') - at('')).toBe(1);
  });
});

describe('auditOps', () => {
  it('a patch is its original 1-based position and its field names, never an id or a name', () => {
    const ops = auditOps(batch([sceneCommands.patch('c', { holdDurationSeconds: 8, name: 'Secret' })]));
    expect(ops).toEqual([{ type: 'patch', sceneIndex: 3, fields: ['holdDurationSeconds', 'name'] }]);
    expect(JSON.stringify(ops)).not.toContain('Secret');
  });

  it('add has no position; delete and duplicate carry the original one', () => {
    const ops = auditOps(batch([sceneCommands.add({ shapeType: 'preset' }), sceneCommands.delete('b', 'Bee'), sceneCommands.duplicate('d', 'Dee')]));
    expect(ops).toEqual([{ type: 'add' }, { type: 'delete', sceneIndex: 2 }, { type: 'duplicate', sceneIndex: 4 }]);
    expect(JSON.stringify(ops)).not.toMatch(/Bee|Dee/);
  });

  it('reorder is read against the running order, and reports the scene\'s ORIGINAL position', () => {
    // a b c d -> move index 3 (d) to the front: d a b c -> move index 3 (now c) to the front
    const ops = auditOps(batch([sceneCommands.reorder(3, 0), sceneCommands.reorder(3, 0)]));
    expect(ops).toEqual([{ type: 'move', sceneIndex: 4 }, { type: 'move', sceneIndex: 3 }]);
  });

  it('stays right after an earlier delete, duplicate and add in the same batch', () => {
    const copy = sceneCommands.duplicate('a', 'A');       // a a' b c d
    const add = sceneCommands.add({});                     // a a' b c d new
    const ops = auditOps(batch([
      copy,
      add,
      sceneCommands.delete('b', 'B'),                      // a a' c d new
      sceneCommands.patch('c', { scale: 2 }),              // c is still original 3
      sceneCommands.reorder(2, 0),                         // c (running index 2) to the front: c a a' d new
      sceneCommands.patch(copy.copyId, { scale: 3 }),      // the copy has no original position
      sceneCommands.patch(add.scene.id, { scale: 3 }),
      sceneCommands.reorder(4, 0),                         // running index 4 is "new": no position
    ]));
    expect(ops.map((o) => [o.type, o.sceneIndex])).toEqual([
      ['duplicate', 1], ['add', undefined], ['delete', 2], ['patch', 3], ['move', 3],
      ['patch', undefined], ['patch', undefined], ['move', undefined],
    ]);
  });

  it('a scene/move swaps in the running order', () => {
    const ops = auditOps(batch([sceneCommands.move(0, 1), sceneCommands.patch('a', { scale: 1 })]));
    expect(ops[0]).toEqual({ type: 'move', sceneIndex: 1 });
    expect(ops[1]).toMatchObject({ type: 'patch', sceneIndex: 1 });
  });

  it('a single non-batch command works, without a position', () => {
    expect(auditOps(sceneCommands.patch('a', { scale: 2 }))).toEqual([{ type: 'patch', fields: ['scale'] }]);
    expect(auditOps(sceneCommands.delete('a'))).toEqual([{ type: 'delete' }]);
  });

  it('anything else is "other"; bad input is an empty list, never a throw', () => {
    expect(auditOps(batch([{ type: 'scene/zoom' }, null]))).toEqual([{ type: 'other' }, { type: 'other' }]);
    for (const bad of [null, undefined, 3, 'x', {}, { type: 'scene/batch' }, { type: 'scene/batch', commands: 5 }]) {
      expect(() => auditOps(bad)).not.toThrow();
    }
    expect(auditOps(null)).toEqual([]);
  });

  it('caps at 20 ops, 10 fields of at most 40 code points, and drops empty names', () => {
    expect(auditOps(batch(Array.from({ length: 25 }, () => sceneCommands.delete('a'))))).toHaveLength(20);
    const many = Object.fromEntries(Array.from({ length: 14 }, (_, i) => [`f${i}`, 1]));
    expect(auditOps(batch([sceneCommands.patch('a', many)]))[0].fields).toHaveLength(10);
    const [long] = auditOps(batch([sceneCommands.patch('a', { [dev(60)]: 1 })]));
    expect([...long.fields[0]]).toHaveLength(40);
    const [e] = auditOps(batch([{ type: 'scene/patch', id: 'a', patch: { '': 1, ok: 2 } }]));
    expect(e.fields).toEqual(['ok']);
  });
});

describe('buildEntry', () => {
  it('a plain entry is exactly the POST body: no server fields, nothing extra', () => {
    const e = buildEntry(base());
    expect(e).toEqual({
      v: 1, requestId: 'req-1', source: 'typed', request: 'set hold of scene 1 to 8 s', summary: 'Edit scene 1: hold 8 s',
      ops: [], check: { status: 'PASSED', kind: 'open', summary: 'Passed all safety checks.' }, decision: 'applied',
    });
    for (const k of FORBIDDEN) expect(e).not.toHaveProperty(k);
  });

  it('leaves out the check when there is none, or it is not a final answer', () => {
    expect(buildEntry(base({ check: undefined }))).not.toHaveProperty('check');
    expect(buildEntry(base({ check: null }))).not.toHaveProperty('check');
    expect(buildEntry(base({ check: { status: 'CHECKING' } }))).not.toHaveProperty('check');
  });

  it('a check with a missing or unknown kind sends null; a REFUSED check has kind null', () => {
    expect(buildEntry(base({ check: { status: 'REFUSED', kind: null, summary: 's' } })).check).toEqual({ status: 'REFUSED', kind: null, summary: 's' });
    expect(buildEntry(base({ check: { status: 'FAILED', summary: 's' } })).check.kind).toBeNull();
    expect(buildEntry(base({ check: { status: 'FAILED', kind: 'bogus', summary: 's' } })).check.kind).toBeNull();
  });

  it('every kind exportGate can return is one the entry accepts', () => {
    const kinds = new Set([
      exportGate({ hasShow: false }).kind,
      exportGate({ hasShow: true, validation: { status: 'PASSED' } }).kind,
      exportGate({ hasShow: true, validation: { status: 'FAILED' } }).kind,
      exportGate({ hasShow: true, validation: null }).kind,
    ]);
    expect(kinds).toEqual(new Set(['none', 'open', 'failed', 'unverified']));
    for (const kind of kinds) expect(buildEntry(base({ check: { status: 'PASSED', kind, summary: 's' } })).check.kind).toBe(kind);
  });

  it('reason is sent only when it has text', () => {
    expect(buildEntry(base({ reason: '' }))).not.toHaveProperty('reason');
    expect(buildEntry(base({ reason: 'scene 3 does not exist' })).reason).toBe('scene 3 does not exist');
  });

  it('the ops come from the command', () => {
    const e = buildEntry(base({ command: batch([sceneCommands.patch('b', { scale: 2 })]) }));
    expect(e.ops).toEqual([{ type: 'patch', sceneIndex: 2, fields: ['scale'] }]);
  });

  it('an unknown decision or source, or a bad requestId, is a programmer error', () => {
    expect(() => buildEntry(base({ decision: 'log_full' }))).toThrow(/decision/);
    expect(() => buildEntry(base({ decision: 'maybe' }))).toThrow(/decision/);
    expect(() => buildEntry(base({ source: 'robot' }))).toThrow(/source/);
    for (const bad of ['', 'a b', 'é', 'x'.repeat(65), 'log_full', undefined, 7]) {
      expect(() => buildEntry(base({ requestId: bad }))).toThrow(/requestId/);
    }
    for (const d of DECISIONS) expect(buildEntry(base({ decision: d })).decision).toBe(d);
  });

  it('cuts every text field at its limit, by code points', () => {
    const e = buildEntry(base({
      request: 'r'.repeat(500), summary: 's'.repeat(500), reason: 'z'.repeat(500),
      check: { status: 'PASSED', kind: 'open', summary: 'c'.repeat(500) },
    }));
    expect([...e.request]).toHaveLength(200);
    expect([...e.summary]).toHaveLength(200);
    expect([...e.reason]).toHaveLength(300);
    expect([...e.check.summary]).toHaveLength(200);
  });

  it('keeps \\n and \\t and removes \\x00, \\x1b and \\r', () => {
    const e = buildEntry(base({ request: 'a\nb\tc\x00d\x1be\rf' }));
    expect(e.request).toBe('a\nb\tc d e f');
    expect(['\x00', '\x1b', '\r'].some((ch) => e.request.includes(ch))).toBe(false);
  });

  it('a lone surrogate never reaches the body', () => {
    const e = buildEntry(base({ request: 'ab\ud83dcd', reason: '\ude00x' }));
    expect(e.request).toBe('abcd');
    expect(e.reason).toBe('x');
  });

  it('CJK fits', () => {
    const e = buildEntry(base({ request: '中'.repeat(200), summary: '日本語'.repeat(60), reason: '한'.repeat(300) }));
    expect(canonicalSize(e)).toBeLessThanOrEqual(BUDGET);
  });

  const worstInput = () => base({
    request: dev(200), summary: dev(200), reason: dev(300),
    check: { status: 'FAILED', kind: 'failed', summary: dev(200) },
  });

  describe('byte budget', () => {
    const worst = () => base({
      request: dev(200), summary: dev(200), reason: dev(300),
      check: { status: 'FAILED', kind: 'failed', summary: dev(200) },
    });

    it('the maximum-length Devanagari entry would be refused unbudgeted, and now fits', () => {
      const w = worst();
      const unbudgeted = {
        v: 1, requestId: w.requestId, source: w.source, request: w.request, summary: w.summary,
        ops: [], check: w.check, decision: w.decision, reason: w.reason,
      };
      expect(canonicalSize(unbudgeted)).toBeGreaterThanOrEqual(LIMITS.entryBytes);
      expect(canonicalSize(buildEntry(w))).toBeLessThanOrEqual(BUDGET);
    });

    it('shrinks reason first, then summary, then check.summary, then request', () => {
      const e = buildEntry(worst());
      // 3600 bytes of request, summary and check summary leave room for only a little of the reason
      expect([...e.reason].length).toBeLessThan(100);
      expect(e.reason.endsWith('…')).toBe(true);
      expect([...e.request]).toHaveLength(200);
      expect([...e.summary]).toHaveLength(200);
      expect([...e.check.summary]).toHaveLength(200);

      // Ops that eat most of the room: the summary gives way next, the other two stay whole.
      const heavy = batch(Array.from({ length: 4 }, () => sceneCommands.patch('a', { [dev(10)]: 1, [dev(11)]: 1, [dev(12)]: 1 })));
      const f = buildEntry({ ...worst(), command: heavy });
      expect(canonicalSize(f)).toBeLessThanOrEqual(BUDGET);
      expect(f).not.toHaveProperty('reason');
      expect([...f.summary].length).toBeLessThan(200);
      expect(f.summary.endsWith('…')).toBe(true);
      expect([...f.check.summary]).toHaveLength(200);
      expect([...f.request]).toHaveLength(200);

      // More still: the check summary gives way, and only then the request.
      const heavier = batch(Array.from({ length: 8 }, () => sceneCommands.patch('a', { [dev(10)]: 1, [dev(11)]: 1, [dev(12)]: 1 })));
      const g = buildEntry({ ...worst(), command: heavier });
      expect(canonicalSize(g)).toBeLessThanOrEqual(BUDGET);
      expect(g.summary).toBe('');
      expect([...g.check.summary].length).toBeLessThan(200);
      expect([...g.request]).toHaveLength(200);
    });

    it('the request is cut only after everything else is gone', () => {
      const cmd = batch(Array.from({ length: 5 }, () => sceneCommands.patch('a', { [dev(40)]: 1, [dev(39)]: 1 })));
      const e = buildEntry({ ...worst(), command: cmd });
      expect(canonicalSize(e)).toBeLessThanOrEqual(BUDGET);
      expect(e.summary).toBe('');
      expect(e.check.summary).toBe('');
      expect([...e.request].length).toBeLessThan(200);
      expect([...e.request].length).toBeGreaterThan(0);
    });

    it('emoji at the cut boundary are never split', () => {
      for (const n of [150, 151, 199, 200]) {
        const e = buildEntry(base({
          request: '😀'.repeat(200), summary: '😀'.repeat(n), reason: '😀'.repeat(300),
          check: { status: 'PASSED', kind: 'open', summary: '😀'.repeat(200) },
        }));
        for (const text of [e.request, e.summary, e.reason ?? '', e.check.summary]) expect(text).not.toMatch(SPLIT_PAIR);
        expect(canonicalSize(e)).toBeLessThanOrEqual(BUDGET);
      }
    });

    it('an emptied reason is omitted, not sent empty; required fields stay present', () => {
      const cmd = batch(Array.from({ length: 4 }, () => sceneCommands.patch('a', { [dev(10)]: 1, [dev(11)]: 1, [dev(12)]: 1 })));
      const e = buildEntry({ ...worst(), command: cmd });
      expect(e).not.toHaveProperty('reason');
      expect(typeof e.request).toBe('string');
      expect(typeof e.summary).toBe('string');
    });

    it('an entry already inside the budget is left untouched', () => {
      const e = buildEntry(base({ request: dev(100), reason: 'because' }));
      expect(e.request).toBe(dev(100));
      expect(e.reason).toBe('because');
    });

    describe('when only the ops are left to give', () => {
      const wide = () => Object.fromEntries(Array.from({ length: 10 }, (_, i) => [`${'k'.repeat(39)}${i}`, 1]));
      const huge = () => batch(Array.from({ length: 20 }, (_, i) => sceneCommands.patch(ids[i % 4], wide())));

      it('20 ops x 10 fields x 40 characters now fit, with the reason saying so', () => {
        const e = buildEntry(base({ command: huge() }));
        expect(canonicalSize(e)).toBeLessThanOrEqual(BUDGET);
        expect(e.reason).toBe('ops shortened');
        // field names give way before any op does: all 20 stay, each with fewer names
        expect(e.ops).toHaveLength(20);
        expect(e.ops.every((op) => (op.fields ?? []).length < 10)).toBe(true);
      });

      it('Devanagari at its maximum together with 20 big ops fits too', () => {
        const dv = batch(Array.from({ length: 20 }, () => sceneCommands.patch('a', Object.fromEntries(
          Array.from({ length: 10 }, (_, i) => [`${dev(39)}${i}`, 1])))));
        const e = buildEntry({ ...worstInput(), command: dv });
        expect(canonicalSize(e)).toBeLessThanOrEqual(BUDGET);
      });

      it('keeps the order, and every kept op and field name is one of the originals', () => {
        const cmd = huge();
        const original = auditOps(cmd);
        const e = buildEntry(base({ command: cmd }));
        e.ops.forEach((op, i) => {
          expect(op.type).toBe(original[i].type);
          expect(op.sceneIndex).toBe(original[i].sceneIndex);
          // the names kept are a prefix of the op's own, never made up
          expect(op.fields ?? []).toEqual((original[i].fields ?? []).slice(0, (op.fields ?? []).length));
        });
      });

      it('cuts field names before it drops any op', () => {
        // 9 ops x 10 names is too big, but fits when each op keeps fewer names: all 9 ops stay.
        const cmd = batch(Array.from({ length: 9 }, () => sceneCommands.patch('a', wide())));
        const e = buildEntry(base({ command: cmd }));
        expect(canonicalSize(e)).toBeLessThanOrEqual(BUDGET);
        expect(e.ops).toHaveLength(9);
        expect(e.ops[0].fields.length).toBeLessThan(10);
        expect(e.reason).toBe('ops shortened');
      });

      it('appends to a reason that is still there, and skips the note when it would not fit', () => {
        // Text gets cut first, so the reason is already gone once the ops have to give: the note stands alone.
        expect(buildEntry(base({ command: huge(), reason: 'kept' })).reason).toBe('ops shortened');
        // Few small ops that fit as they are: nothing is shortened and no note is added.
        const small = buildEntry(base({ command: batch([sceneCommands.patch('a', { scale: 1 })]), reason: 'kept' }));
        expect(small.reason).toBe('kept');
      });

      it('still throws for a programmer error, and only for that', () => {
        expect(() => buildEntry(base({ command: huge(), decision: 'nope' }))).toThrow(/decision/);
        expect(() => buildEntry(base({ command: huge(), requestId: '' }))).toThrow(/requestId/);
        expect(() => buildEntry(base({ command: huge() }))).not.toThrow();
      });
    });
  });
});

describe('limits agree with the backend (audit_log.py)', () => {
  const py = readFileSync(new URL('../../../tara-studio/tara_studio/audit_log.py', import.meta.url), 'utf8');
  const num = (re) => {
    const m = py.match(re);
    if (!m) throw new Error(`audit_log.py no longer matches ${re}`);
    return Number(m[1]);
  };
  const words = (re) => {
    const m = py.match(re);
    if (!m) throw new Error(`audit_log.py no longer matches ${re}`);
    return [...m[1].matchAll(/"(\w+)"/g)].map((x) => x[1]);
  };

  it('field lengths, list sizes and the entry size', () => {
    expect(num(/request: str = Field\(\.\.\., max_length=(\d+)\)/)).toBe(LIMITS.request);
    expect(num(/summary: str = Field\(\.\.\., max_length=(\d+)\)\s+ops: /)).toBe(LIMITS.summary);
    expect(num(/reason: str \| None = Field\(None, max_length=(\d+)\)/)).toBe(LIMITS.reason);
    expect(num(/class AuditCheck[\s\S]*?summary: str = Field\(\.\.\., max_length=(\d+)\)/)).toBe(LIMITS.checkSummary);
    expect(num(/ops: list\[AuditOp\] = Field\(default_factory=list, max_length=(\d+)\)/)).toBe(LIMITS.ops);
    expect(num(/StringConstraints\(max_length=\d+\)\]\] = Field\(default_factory=list, max_length=(\d+)\)/)).toBe(LIMITS.fieldsPerOp);
    expect(num(/StringConstraints\(max_length=(\d+)\)/)).toBe(LIMITS.fieldName);
    expect(num(/^MAX_ENTRY_BYTES = (\d+)/m)).toBe(LIMITS.entryBytes);
  });

  it('is still measured as canonical JSON with ensure_ascii, and refused at >= the limit', () => {
    expect(py).toContain('ensure_ascii=True');
    expect(py).toContain('sort_keys=True, separators=(",", ":")');
    expect(py).toContain('len(_canonical(self.model_dump())) >= MAX_ENTRY_BYTES');
    expect(py).toContain('_REQUEST_ID_RE = r"^[A-Za-z0-9_-]{1,64}$"');
  });

  it('decisions, sources, check statuses and kinds', () => {
    expect(words(/decision: Literal\[([\s\S]*?)\]/)).toEqual(DECISIONS);
    expect(words(/source: Literal\[([^\]]*)\]/)).toEqual(['typed', 'planner']);
    expect(words(/status: Literal\[([^\]]*)\]/)).toEqual(['PASSED', 'FAILED', 'REFUSED', 'UNVERIFIED']);
    expect(words(/CHECK_KINDS = \(([^)]*)\)/)).toEqual(['none', 'open', 'failed', 'unverified']);
  });
});
