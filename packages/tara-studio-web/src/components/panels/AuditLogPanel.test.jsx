import { describe, expect, it, vi, beforeEach } from 'vitest';
import { findAll, textOf, makeStateStub } from '../../testing/elementTree.js';

// No DOM here (see ProposalPanel.test.jsx): the real panel runs with the state hooks stubbed
// and useEffect collected, so each state can be rendered and each effect run by hand.
const stub = makeStateStub();
let effects = [];
vi.mock('react', async (orig) => ({
  ...(await orig()),
  useState: (i) => stub.useState(i),
  useRef: (init) => {
    const i = stub.i++;
    if (!(i in stub.slots)) stub.slots[i] = { current: init };
    return stub.slots[i];
  },
  useEffect: (fn, deps) => { effects.push({ fn, deps }); },
}));

const { default: AuditLogPanel } = await import('./AuditLogPanel.jsx');
const { MESSAGES } = await import('../../engine/auditView.js');

const entry = (n, over = {}) => ({
  v: 1, seq: n, at: '2026-11-16T09:05:07.000Z', source: 'typed', request: `request ${n}`, summary: `summary ${n}`,
  ops: [], check: { status: 'PASSED', kind: 'open', summary: 's' }, decision: 'applied', reason: null, ...over,
});
const ok = (entries, over = {}) => ({ ok: true, entries, total: entries.length, full: false, chainOk: true, torn: false, corruptLines: 0, ...over });
const cfg = { intent: {}, client: null };
let cleanups = [];
const render = () => {
  stub.i = 0;
  effects = [];
  return AuditLogPanel({ intent: cfg.intent, client: cfg.client });
};
// Renders, runs this render's effect, and lets the read finish.
const mount = async () => {
  const tree = render();
  cleanups.push(...effects.map((e) => e.fn()));
  await new Promise((r) => setTimeout(r, 0));
  return tree;
};
const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  stub.slots = [];
  cleanups = [];
  cfg.intent = { auditNote: null, auditVersion: 0, auditProjectId: 'proj-1' };
  cfg.client = { list: vi.fn(async () => ok([entry(2), entry(1, { decision: 'dismissed' })])) };
});

describe('the states', () => {
  it('no saved project', async () => {
    cfg.intent.auditProjectId = null;
    await mount();
    const tree = render();
    expect(textOf(tree)).toContain(MESSAGES.noProject);
    expect(cfg.client.list).not.toHaveBeenCalled();
  });

  it('loading, then the rows newest first, as a labelled ordered list of items', async () => {
    let finish;
    cfg.client.list = vi.fn(() => new Promise((r) => { finish = r; }));
    const pending = mount();
    expect(textOf(render())).toContain(MESSAGES.loading);
    finish(ok([entry(2), entry(1, { decision: 'dismissed' })]));
    await pending;
    await flush();
    const tree = render();
    const [list] = findAll(tree, (n) => n.type === 'ol');
    expect(list.props['aria-label']).toBe('AI decision log');
    const rows = findAll(tree, (n) => n.type === 'li');
    expect(rows).toHaveLength(2);
    expect(textOf(rows[0])).toContain('Applied');
    expect(textOf(rows[0])).toContain('request 2');
    expect(textOf(rows[1])).toContain('Dismissed');
    expect(textOf(tree)).toContain('AI decision log');
    expect(cfg.client.list).toHaveBeenCalledWith('proj-1', 20);
  });

  it('empty', async () => {
    cfg.client.list = vi.fn(async () => ok([]));
    await mount();
    expect(textOf(render())).toContain(MESSAGES.empty);
    expect(findAll(render(), (n) => n.type === 'ol')).toHaveLength(0);
  });

  it('error', async () => {
    cfg.client.list = vi.fn(async () => ({ ok: false, code: 'network', status: 0 }));
    await mount();
    expect(textOf(render())).toContain(MESSAGES.error);
  });

  it('a failed integrity check and a full log are quiet status lines above the rows', async () => {
    cfg.client.list = vi.fn(async () => ok([entry(1)], { chainOk: false, full: true }));
    await mount();
    const notices = findAll(render(), (n) => n.props?.['data-testid'] === 'audit-log-notice');
    expect(notices.map(textOf)).toEqual([MESSAGES.integrity, MESSAGES.full]);
    for (const n of notices) expect(n.props.role).toBe('status');
  });
});

describe('the note from the hook', () => {
  it('is one quiet status line, not an alert, and never red', () => {
    cfg.intent.auditNote = { kind: 'unconfirmed', message: 'Could not confirm the audit entry was saved.' };
    const tree = render();
    const [note] = findAll(tree, (n) => n.props?.['data-testid'] === 'audit-note');
    expect(note.props.role).toBe('status');
    expect(textOf(note)).toBe('Could not confirm the audit entry was saved.');
    expect(findAll(tree, (n) => n.props?.role === 'alert')).toHaveLength(0);
    expect(findAll(tree, (n) => /failed|danger/.test(n.props?.className ?? ''))).toHaveLength(0);
  });

  it('a skipped note is plain', () => {
    cfg.intent.auditNote = { kind: 'skipped', message: 'Not recorded: no saved project yet.' };
    const [note] = findAll(render(), (n) => n.props?.['data-testid'] === 'audit-note');
    expect(note.props.className).toBe('audit-note');
  });

  it('no note, no line', () => {
    expect(findAll(render(), (n) => n.props?.['data-testid'] === 'audit-note')).toHaveLength(0);
  });
});

describe('read-only, and text only', () => {
  it('has no button, input, textarea, select or link, in any state', async () => {
    cfg.intent.auditNote = { kind: 'rejected', message: 'x' };
    cfg.client.list = vi.fn(async () => ok([entry(1, { request: '<b>x</b>' })], { full: true }));
    await mount();
    const tree = render();
    expect(findAll(tree, (n) => ['button', 'input', 'textarea', 'select', 'a', 'form'].includes(n.type))).toHaveLength(0);
    expect(findAll(tree, (n) => n.props && ('onClick' in n.props || 'onChange' in n.props || 'onSubmit' in n.props))).toHaveLength(0);
  });

  it('never uses dangerouslySetInnerHTML: markup in an entry is a plain text node', async () => {
    cfg.client.list = vi.fn(async () => ok([entry(1, { request: '<img src=x onerror=alert(1)>', summary: '<script>x</script>' })]));
    await mount();
    const tree = render();
    expect(findAll(tree, (n) => n.props && 'dangerouslySetInnerHTML' in n.props)).toHaveLength(0);
    expect(textOf(tree)).toContain('<img src=x onerror=alert(1)>');
  });

  it('long Devanagari, CJK and emoji render whole, in wrapping text elements', async () => {
    const long = `${'क'.repeat(200)}${'中'.repeat(60)}${'😀'.repeat(40)}`;
    cfg.client.list = vi.fn(async () => ok([entry(1, { request: long })]));
    await mount();
    const [p] = findAll(render(), (n) => n.type === 'p' && n.props.className.includes('audit-text'));
    expect(textOf(p)).toBe(long);
  });
});

describe('when it reads', () => {
  it('again when the version moves, and again when the project changes; never for the same inputs', async () => {
    await mount();
    expect(cfg.client.list).toHaveBeenCalledTimes(1);
    cfg.intent = { ...cfg.intent, auditVersion: 1 };
    render();
    expect(effects[0].deps).toEqual(['proj-1', 1, cfg.client]);
    cfg.intent = { ...cfg.intent, auditProjectId: 'proj-2' };
    render();
    expect(effects[0].deps).toEqual(['proj-2', 1, cfg.client]);
  });

  it('a slower, older answer never overwrites a newer one', async () => {
    let first;
    let second;
    cfg.client.list = vi.fn()
      .mockImplementationOnce(() => new Promise((r) => { first = r; }))
      .mockImplementationOnce(() => new Promise((r) => { second = r; }));
    render();
    const run = effects.map((e) => e.fn());          // the read for version 0
    cfg.intent = { ...cfg.intent, auditVersion: 1 };
    render();
    run.forEach((c) => c?.());                        // React runs the old effect's cleanup first
    effects.map((e) => e.fn());                       // the read for version 1
    second(ok([entry(9, { request: 'the newer answer' })]));
    await flush();
    first(ok([entry(1, { request: 'the older answer' })]));
    await flush();
    const text = textOf(render());
    expect(text).toContain('the newer answer');
    expect(text).not.toContain('the older answer');
  });

  it('an answer that arrives after the panel is gone is ignored', async () => {
    let finish;
    cfg.client.list = vi.fn(() => new Promise((r) => { finish = r; }));
    render();
    const cleanup = effects.map((e) => e.fn());
    cleanup.forEach((c) => c?.());                    // unmount
    finish(ok([entry(1, { request: 'too late' })]));
    await flush();
    expect(textOf(render())).not.toContain('too late');
  });

  it('keeps the rows of the same project on screen while it reads again; another project\'s rows go', async () => {
    await mount();
    cfg.client.list = vi.fn(() => new Promise(() => {}));
    cfg.intent = { ...cfg.intent, auditVersion: 1 };
    render();
    effects.map((e) => e.fn());
    expect(textOf(render())).toContain('request 2');
    cfg.intent = { ...cfg.intent, auditProjectId: 'proj-2' };
    render();
    effects.map((e) => e.fn());
    expect(textOf(render())).not.toContain('request 2');
    expect(textOf(render())).toContain(MESSAGES.loading);
  });
});
