import { describe, expect, it, vi, beforeEach } from 'vitest';
import { makeStateStub } from '../testing/elementTree.js';
import { makeScene } from '../engine/sequencer.js';

// The audit recording of the proposal flow (issue #94). Same no-DOM approach as
// ProposalPanel.test.jsx: the real hook runs with useState and useRef stubbed. The audit
// client is a vi.fn, the planner and the safety check are mocked, and every decision the flow
// can end in is driven through the hook's own functions.
const stub = makeStateStub();
vi.mock('react', async (orig) => ({
  ...(await orig()),
  useState: (i) => stub.useState(i),
  useRef: (init) => {
    const i = stub.i++;
    if (!(i in stub.slots)) stub.slots[i] = { current: init };
    return stub.slots[i];
  },
  useCallback: (fn) => fn,
}));
const checkProposal = vi.fn();
vi.mock('../engine/proposalCheck.js', () => ({
  checkProposal: (...a) => checkProposal(...a), buildForCheck: vi.fn(), validateForCheck: vi.fn(),
}));
const proposePlan = vi.fn();
vi.mock('../engine/api.js', async (orig) => ({ ...(await orig()), proposePlan: (...a) => proposePlan(...a) }));
const buildEntryMock = vi.fn();
vi.mock('../engine/auditEntry.js', async (orig) => ({ ...(await orig()), buildEntry: (...a) => buildEntryMock(...a) }));
const actual = await vi.importActual('../engine/auditEntry.js');

const { default: intentHook } = await import('./useIntentProposal.js');

const SENTENCE = 'set hold of scene 1 to 8 s';
const mkDoc = () => ({
  scenes: [makeScene({ id: 'a', name: 'One' }), makeScene({ id: 'b', name: 'Two' })],
  dronesList: [],
});
const PASSED = { status: 'PASSED', summary: 'all good', violations: {}, kind: 'open' };
const cfg = { projectId: 'proj-1', client: null, doc: mkDoc(), execute: null, order: [] };
const render = () => {
  stub.i = 0;
  return intentHook({
    doc: cfg.doc, execute: cfg.execute, numDrones: 6, rate: 4, pacing: 'standard',
    projectId: cfg.projectId, auditClient: cfg.client,
  });
};
const flush = () => new Promise((r) => setTimeout(r, 0));
const appended = () => cfg.client.append.mock.calls.map(([projectId, entry]) => ({ projectId, ...entry }));
const decisions = () => appended().map((e) => e.decision);

const typed = async (sentence = SENTENCE) => { render().setText(sentence); await render().preview(); await flush(); };
const planned = async (sentence = 'make the second scene a bigger sphere') => { render().setText(sentence); await render().plan(); await flush(); };
const GOOD = { status: 'ok', ops: [{ op: 'patch', scene: 2, shape_type: 'parametric', shape_payload: 'sphere', scale: 2 }], unsupported: [] };

beforeEach(() => {
  stub.slots = [];
  cfg.projectId = 'proj-1';
  cfg.doc = mkDoc();
  cfg.order = [];
  cfg.execute = vi.fn(() => cfg.order.push('execute'));
  cfg.client = { append: vi.fn(async () => { cfg.order.push('append'); return { ok: true, entry: {} }; }) };
  checkProposal.mockReset();
  checkProposal.mockResolvedValue(PASSED);
  proposePlan.mockReset();
  buildEntryMock.mockReset();
  buildEntryMock.mockImplementation((...a) => actual.buildEntry(...a));
});

describe('each decision is recorded exactly once, with its source', () => {
  const cases = {
    applied: async () => { await typed(); render().apply(); },
    dismissed: async () => { await typed(); render().dismiss(); },
    stale: async () => { await typed(); cfg.doc = { ...mkDoc(), scenes: [makeScene({ id: 'a', name: 'Changed' }), makeScene({ id: 'b', name: 'Two' })] }; render().apply(); },
    blocked: async () => { checkProposal.mockResolvedValue({ ...PASSED, status: 'FAILED', kind: 'failed' }); await typed(); render().apply(); },
    superseded: async () => { await typed(); render().setText('set hold of scene 2 to 5 s'); await render().preview(); },
    refused: async () => { await typed('please do something odd'); },
    planner_unavailable: async () => { proposePlan.mockResolvedValue({ status: 'unavailable', ops: [], unsupported: [] }); await planned(); },
    planner_timeout: async () => { proposePlan.mockResolvedValue({ status: 'timeout', ops: [], unsupported: [] }); await planned(); },
    planner_failed: async () => { proposePlan.mockRejectedValue(new Error('boom')); await planned(); },
  };
  const source = { planner_unavailable: 'planner', planner_timeout: 'planner', planner_failed: 'planner' };

  it.each(Object.keys(cases))('%s', async (decision) => {
    await cases[decision]();
    await flush();
    const mine = appended().filter((e) => e.decision === decision);
    expect(mine).toHaveLength(1);
    expect(mine[0]).toMatchObject({ v: 1, decision, source: source[decision] ?? 'typed', projectId: 'proj-1' });
    expect(mine[0].requestId).toMatch(/^[A-Za-z0-9_-]{1,64}$/);
    for (const k of ['seq', 'at', 'prev', 'hash']) expect(mine[0]).not.toHaveProperty(k);
  });

  it('the planner paths that refuse (unsupported, a draft refused by compile, a schema failure twice) are "refused"', async () => {
    proposePlan.mockResolvedValue({ status: 'ok', ops: [], unsupported: ['teleport is not supported'] });
    await planned();
    proposePlan.mockResolvedValue({ status: 'ok', ops: [{ op: 'patch', scene: 2, scale: 9 }], unsupported: [] });
    await planned();
    proposePlan.mockResolvedValue({ status: 'schema_error', ops: [], unsupported: [], error: 'bad shape' });
    await planned();
    expect(decisions()).toContain('refused');
    const refused = appended().filter((e) => e.decision === 'refused');
    expect(refused).toHaveLength(3);
    for (const e of refused) {
      expect(e).toMatchObject({ source: 'planner' });
      expect(e.ops).toEqual([]);
      expect(e).not.toHaveProperty('check');
      expect(e.reason.length).toBeGreaterThan(0);
    }
  });

  it('a typed refusal carries the reason the operator sees, and no check or ops', async () => {
    await typed('please do something odd');
    const [e] = appended();
    expect(e).toMatchObject({ decision: 'refused', source: 'typed', request: 'please do something odd', ops: [] });
    expect(e.reason).toBe(render().error);
    expect(e).not.toHaveProperty('check');
  });

  it('a planned proposal that is applied is recorded as source planner', async () => {
    proposePlan.mockResolvedValue(GOOD);
    await planned();
    render().apply();
    expect(appended()).toMatchObject([{ decision: 'applied', source: 'planner', request: 'make the second scene a bigger sphere' }]);
  });
});

describe('what an entry carries', () => {
  it('applied has the check (status, kind, summary) and ops that name no id and no scene name', async () => {
    await typed();
    render().apply();
    const [e] = appended();
    expect(e.check).toEqual({ status: 'PASSED', kind: 'open', summary: 'all good' });
    expect(e.ops).toEqual([{ type: 'patch', sceneIndex: 1, fields: ['holdDurationSeconds'] }]);
    const opsText = JSON.stringify(e.ops);
    expect(opsText).not.toMatch(/"a"|"b"|One|Two/);
  });

  it('the request text is the text at request time, not the input as it is when the operator decides', async () => {
    await typed();
    render().setText('something else entirely');
    render().apply();
    expect(appended()[0].request).toBe(SENTENCE);
    await typed('set hold of scene 2 to 5 s');
    render().setText('and another thing');
    render().dismiss();
    expect(appended()[1].request).toBe('set hold of scene 2 to 5 s');
  });

  it('two requests have two different requestIds, and one request keeps its id', async () => {
    await typed();
    const first = render().proposal.requestId;
    render().apply();
    await typed('set hold of scene 2 to 5 s');
    const second = render().proposal.requestId;
    render().apply();
    expect(first).not.toBe(second);
    expect(appended().map((e) => e.requestId)).toEqual([first, second]);
  });

  it('the proposal stores its request: id, source, text as sent, project', async () => {
    await typed();
    expect(render().proposal).toMatchObject({ source: 'typed', requestText: SENTENCE, projectId: 'proj-1' });
  });
});

describe('the edit always comes first and never waits', () => {
  it('Apply executes the edit BEFORE the entry is appended', async () => {
    await typed();
    render().apply();
    expect(cfg.order).toEqual(['execute', 'append']);
  });

  it('Apply does not wait for the append: the edit is done and the proposal gone while it is still pending', async () => {
    cfg.client.append = vi.fn(() => new Promise(() => {}));
    await typed();
    render().apply();
    expect(cfg.execute).toHaveBeenCalledTimes(1);
    expect(render().proposal).toBeNull();
  });

  const failures = {
    '404': { ok: false, code: 'project_not_found', status: 404, notStored: true },
    '409': { ok: false, code: 'audit_full', status: 409, notStored: true },
    '422': { ok: false, code: 'invalid_entry', status: 422, notStored: true },
    '503': { ok: false, code: 'vault_unavailable', status: 503, notStored: false },
    network: { ok: false, code: 'network', status: 0, notStored: false },
    timeout: { ok: false, code: 'timeout', status: 0, notStored: false },
  };
  it.each(Object.entries(failures))('an append that answers %s still leaves the edit applied', async (_name, result) => {
    cfg.client.append = vi.fn(async () => result);
    await typed();
    render().apply();
    await flush();
    expect(cfg.execute).toHaveBeenCalledTimes(1);
    expect(render().proposal).toBeNull();
    expect(render().auditNote).toMatchObject({ kind: result.notStored ? 'rejected' : 'unconfirmed' });
  });

  it('an append that rejects, throws at once, or comes from a buildEntry that throws: the edit is still applied', async () => {
    for (const make of [
      () => { cfg.client.append = vi.fn(() => Promise.reject(new Error('rejected'))); },
      () => { cfg.client.append = vi.fn(() => { throw new Error('sync'); }); },
      () => { buildEntryMock.mockImplementation(() => { throw new Error('programmer error'); }); },
    ]) {
      stub.slots = [];
      cfg.execute = vi.fn();
      cfg.client = { append: vi.fn(async () => ({ ok: true })) };
      buildEntryMock.mockImplementation((...a) => actual.buildEntry(...a));
      make();
      await typed();
      expect(() => render().apply()).not.toThrow();
      await flush();
      expect(cfg.execute).toHaveBeenCalledTimes(1);
      expect(render().proposal).toBeNull();
      expect(render().auditNote).not.toBeNull();
    }
  });

  it('an entry that cannot be built is not sent, and the note says it was not recorded', async () => {
    buildEntryMock.mockImplementation(() => { throw new Error('programmer error'); });
    await typed();
    render().apply();
    await flush();
    expect(cfg.client.append).not.toHaveBeenCalled();
    expect(render().auditNote).toEqual({ kind: 'rejected', code: 'internal', message: 'The audit entry could not be built; this decision was not recorded.' });
    expect(cfg.execute).toHaveBeenCalledTimes(1);
  });

  it('dismiss, preview and plan are not held up by a pending append either', async () => {
    cfg.client.append = vi.fn(() => new Promise(() => {}));
    await typed();
    render().dismiss();
    expect(render().proposal).toBeNull();
    await typed();
    render().setText('set hold of scene 2 to 5 s');
    await render().preview();
    expect(render().proposal).not.toBeNull();
  });

  it('the notes say what happened, in plain words', async () => {
    cfg.client.append = vi.fn(async () => failures['409']);
    await typed(); render().apply(); await flush();
    expect(render().auditNote).toEqual({ kind: 'rejected', code: 'audit_full', message: 'The audit log for this project is full; this decision was not recorded.' });
    cfg.client.append = vi.fn(async () => failures['404']);
    await typed('set hold of scene 2 to 5 s'); render().apply(); await flush();
    expect(render().auditNote).toMatchObject({ kind: 'rejected', code: 'project_not_found' });
    expect(render().auditNote.message).toMatch(/no longer in the Project Vault/);
    cfg.client.append = vi.fn(async () => failures['503']);
    await typed('set hold of scene 1 to 6 s'); render().apply(); await flush();
    expect(render().auditNote).toMatchObject({ kind: 'unconfirmed', message: 'Could not confirm the audit entry was saved.' });
  });

  it('a failure is never retried: one append per decision', async () => {
    cfg.client.append = vi.fn(async () => failures['503']);
    await typed();
    render().apply();
    await flush();
    expect(cfg.client.append).toHaveBeenCalledTimes(1);
  });

  it('auditNote is cleared when the next request starts, and auditVersion moves after every attempt', async () => {
    cfg.client.append = vi.fn(async () => failures['503']);
    await typed();
    expect(render().auditVersion).toBe(0);
    render().apply();
    await flush();
    expect(render().auditNote).not.toBeNull();
    expect(render().auditVersion).toBe(1);
    cfg.client.append = vi.fn(async () => ({ ok: true }));
    render().setText('set hold of scene 2 to 5 s');
    const pending = render().preview();
    expect(render().auditNote).toBeNull();
    await pending;
    render().apply();
    await flush();
    expect(render().auditVersion).toBe(2);
  });
});

describe('no project to record in', () => {
  it.each([null, undefined, ''])('projectId %j: nothing is posted, a skipped note is set, the edit is applied', async (id) => {
    cfg.projectId = id;
    await typed();
    render().apply();
    await flush();
    expect(cfg.client.append).not.toHaveBeenCalled();
    expect(render().auditNote).toEqual({ kind: 'skipped', message: 'Not recorded: no saved project yet.' });
    expect(cfg.execute).toHaveBeenCalledTimes(1);
  });

  it('a hook that is given no client and no project never touches fetch', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    cfg.client = null;
    cfg.projectId = null;
    await typed();
    render().apply();
    await flush();
    expect(fetchSpy).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });
});

describe('the project of the request, not the project open later', () => {
  it('an entry goes to the project that was open when the request was made', async () => {
    await typed();
    cfg.projectId = 'proj-2';
    render().apply();
    await typed('set hold of scene 2 to 5 s');
    render().apply();
    expect(appended().map((e) => e.projectId)).toEqual(['proj-1', 'proj-2']);
  });

  it('a proposal made while no project was open is not recorded in the one opened afterwards', async () => {
    cfg.projectId = null;
    await typed();
    cfg.projectId = 'proj-2';
    render().apply();
    expect(cfg.client.append).not.toHaveBeenCalled();
  });
});

describe('superseded and dismissed requests belong to the OLD request', () => {
  it('a new preview replaces an open proposal: the entry has the old id, text, check and project', async () => {
    await typed();
    const old = render().proposal;
    cfg.projectId = 'proj-2';
    render().setText('set hold of scene 2 to 5 s');
    await render().preview();
    await flush();
    const [e] = appended();
    expect(e).toMatchObject({ decision: 'superseded', requestId: old.requestId, request: SENTENCE, projectId: 'proj-1', source: 'typed' });
    expect(e.check).toMatchObject({ status: 'PASSED', kind: 'open' });
    expect(render().proposal.requestId).not.toBe(old.requestId);
  });

  it('a planner request replacing an open proposal supersedes the proposal, and a typed preview that takes over an in-flight planner call supersedes that call', async () => {
    await typed();
    const typedId = render().proposal.requestId;
    let finish;
    proposePlan.mockReturnValue(new Promise((r) => { finish = r; }));
    render().setText('make the second scene a bigger sphere');
    const pending = render().plan();
    await flush();
    expect(appended().map((e) => [e.decision, e.requestId])).toEqual([['superseded', typedId]]);
    render().setText('set hold of scene 2 to 5 s');
    await render().preview();
    await flush();
    expect(appended().map((e) => [e.decision, e.source])).toEqual([['superseded', 'typed'], ['superseded', 'planner']]);
    expect(appended()[1].request).toBe('make the second scene a bigger sphere');
    // The dropped call answers late: it is neither shown nor logged.
    finish({ status: 'unavailable', ops: [], unsupported: [] });
    await pending;
    await flush();
    expect(appended()).toHaveLength(2);
  });

  it('dismissing a planner call in flight logs "dismissed" once, and its late answers are never logged as planner_*', async () => {
    for (const status of ['unavailable', 'timeout']) {
      stub.slots = [];
      cfg.client.append.mockClear();
      let finish;
      proposePlan.mockReturnValue(new Promise((r) => { finish = r; }));
      render().setText('make the second scene a bigger sphere');
      const pending = render().plan();
      render().dismiss();
      finish({ status, ops: [], unsupported: [] });
      await pending;
      await flush();
      expect(decisions()).toEqual(['dismissed']);
      expect(appended()[0].source).toBe('planner');
    }
  });

  it('a planner call that fails after being replaced is not logged as planner_failed', async () => {
    let fail;
    proposePlan.mockReturnValue(new Promise((_r, rej) => { fail = rej; }));
    render().setText('make the second scene a bigger sphere');
    const pending = render().plan();
    await typed('set hold of scene 2 to 5 s');
    fail(new Error('late'));
    await pending;
    await flush();
    expect(decisions()).toEqual(['superseded']);
  });
});

describe('once per request', () => {
  it('a double click on Apply logs "applied" once', async () => {
    await typed();
    const { apply } = render();
    apply();
    apply();
    await flush();
    expect(decisions()).toEqual(['applied']);
  });

  it('an applied request is not also logged as dismissed or superseded', async () => {
    await typed();
    const stale = render();           // a handler captured before the apply
    render().apply();
    stale.dismiss();
    await typed('set hold of scene 2 to 5 s');
    await flush();
    expect(decisions()).toEqual(['applied']);
  });

  it('a request blocked many times for the same reason is logged once; a different reason is logged again', async () => {
    checkProposal.mockResolvedValue({ ...PASSED, status: 'FAILED', kind: 'failed' });
    await typed();
    for (let i = 0; i < 4; i += 1) render().apply();
    await flush();
    expect(decisions()).toEqual(['blocked']);
    cfg.doc = { ...mkDoc(), scenes: [makeScene({ id: 'a', name: 'Changed' }), makeScene({ id: 'b', name: 'Two' })] };
    render().apply();
    render().apply();
    await flush();
    expect(decisions()).toEqual(['blocked', 'stale']);
  });

  it('a blocked request can still be applied afterwards, and is then logged as applied', async () => {
    let finish;
    checkProposal.mockReturnValue(new Promise((r) => { finish = r; }));
    render().setText(SENTENCE);
    const pending = render().preview();
    await flush();
    render().apply();                           // the check is still running: blocked
    finish(PASSED);
    await pending;
    await flush();
    render().apply();
    await flush();
    expect(decisions()).toEqual(['blocked', 'applied']);
  });

  it('the memory of logged requests is trimmed: it cannot grow forever', async () => {
    for (let i = 0; i < 230; i += 1) {
      render().setText('please do something odd');
      await render().preview();
    }
    await flush();
    expect(decisions()).toHaveLength(230);
    // a long run neither stalls nor throws, and ids are all distinct
    expect(new Set(appended().map((e) => e.requestId)).size).toBe(230);
  });
});

describe('the public API of the hook is unchanged', () => {
  it('still has every member the panel uses, and adds the two audit members', () => {
    const intent = render();
    for (const k of ['text', 'setText', 'proposal', 'check', 'error', 'block', 'preview', 'apply', 'dismiss', 'plan', 'planning', 'planStatus']) {
      expect(intent).toHaveProperty(k);
    }
    expect(intent.auditNote).toBeNull();
    expect(intent.auditVersion).toBe(0);
  });
});
