import { describe, expect, it, vi, beforeEach } from 'vitest';
import { button, findAll, textOf, makeStateStub } from '../../testing/elementTree.js';
import { makeScene } from '../../engine/sequencer.js';

// Same no-DOM approach as ProposalPanel.test.jsx: the real hook runs with useState
// and useRef stubbed, so the panel it feeds can be rendered and clicked directly.
// The model (proposePlan) and the safety check are mocked; nothing is networked.
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
vi.mock('../../engine/proposalCheck.js', () => ({
  checkProposal: (...a) => checkProposal(...a), buildForCheck: vi.fn(), validateForCheck: vi.fn(),
}));
const proposePlan = vi.fn();
vi.mock('../../engine/api.js', async (orig) => ({ ...(await orig()), proposePlan: (...a) => proposePlan(...a) }));

const { default: intentHook } = await import('../../hooks/useIntentProposal.js');
const { default: ProposalPanel } = await import('./ProposalPanel.jsx');

const mkDoc = () => ({
  scenes: [makeScene({ id: 'a', name: 'One' }), makeScene({ id: 'b', name: 'Two' })],
  dronesList: [],
});
const state = { doc: mkDoc(), execute: vi.fn(), numDrones: 6, rate: 4, pacing: 'standard' };
const render = (over = {}) => {
  stub.i = 0;
  const intent = intentHook({ ...state, ...over });
  return { intent, tree: ProposalPanel({ intent }) };
};
const SENTENCE = 'make the second scene a bigger sphere';
const ok = (ops, unsupported = []) => ({ status: 'ok', ops, unsupported });
const GOOD = ok([{ op: 'patch', scene: 2, shape_type: 'parametric', shape_payload: 'sphere', scale: 2 }]);
const TOO_BIG = ok([{ op: 'patch', scene: 2, scale: 9 }]);

const ask = async (over = {}) => {
  render(over).intent.setText(SENTENCE);
  await render(over).intent.plan();
  return render(over);
};

beforeEach(() => {
  stub.slots = [];
  state.doc = mkDoc();
  state.execute = vi.fn();
  checkProposal.mockReset();
  proposePlan.mockReset();
  checkProposal.mockResolvedValue({ status: 'PASSED', summary: 'all good', violations: {} });
});

describe('Ask planner button', () => {
  it('sits beside Preview edit, disabled while the request is empty', () => {
    const { tree } = render();
    expect(button(tree, 'Preview edit').props.disabled).toBe(true);
    expect(button(tree, 'Ask planner').props.disabled).toBe(true);
    render().intent.setText('  ');
    expect(button(render().tree, 'Ask planner').props.disabled).toBe(true);
    render().intent.setText(SENTENCE);
    expect(button(render().tree, 'Ask planner').props.disabled).toBe(false);
  });

  it('is disabled and reads Planning… while the model is working', async () => {
    let finish;
    proposePlan.mockReturnValue(new Promise((r) => { finish = r; }));
    render().intent.setText(SENTENCE);
    const pending = render().intent.plan();
    const { tree } = render();
    expect(button(tree, 'Planning…').props.disabled).toBe(true);
    expect(findAll(tree, (n) => n.props['data-testid'] === 'edit-planning')).toHaveLength(1);
    finish(GOOD);
    await pending;
    expect(button(render().tree, 'Ask planner').props.disabled).toBe(false);
  });

  it('is disabled while the safety check runs', async () => {
    proposePlan.mockResolvedValue(GOOD);
    checkProposal.mockReturnValue(new Promise(() => {}));
    render().intent.setText(SENTENCE);
    render().intent.plan();
    await new Promise((r) => setTimeout(r, 0));
    expect(button(render().tree, 'Ask planner').props.disabled).toBe(true);
  });

  it('keeps the existing labels and the hint mentions both', () => {
    const { tree } = render();
    expect(button(tree, 'Preview edit')).toBeDefined();
    expect(textOf(tree)).toMatch(/Preview edit.*Ask planner/s);
    expect(findAll(tree, (n) => n.props['data-testid'] === 'edit-panel')).toHaveLength(1);
  });
});

describe('a good draft', () => {
  it('goes through the same proposal, check and Apply as a typed edit - one undo step', async () => {
    proposePlan.mockResolvedValue(GOOD);
    const { tree, intent } = await ask();
    expect(proposePlan).toHaveBeenCalledTimes(1);
    const sent = proposePlan.mock.calls[0][0];
    expect(sent.prompt).toBe(SENTENCE);
    expect(sent.repairNote).toBeNull();
    expect(sent.scenes.map((s) => s.number)).toEqual([1, 2]);
    expect(JSON.stringify(sent)).not.toMatch(/"(a|b)"/); // no ids leave the browser

    expect(intent.proposal.command.type).toBe('scene/batch');
    expect(checkProposal).toHaveBeenCalledTimes(1);
    expect(textOf(tree)).toContain('Planner: Edit scene 2 ("Two")');
    expect(textOf(tree)).toContain('PASSED: all good');

    button(tree, 'Apply edit').props.onClick();
    expect(state.execute).toHaveBeenCalledTimes(1);
    expect(state.execute.mock.calls[0][0].type).toBe('scene/batch');
    expect(render().intent.proposal).toBeNull();
  });

  it('cannot be applied unless the check PASSED, however the draft looks', async () => {
    checkProposal.mockResolvedValue({ status: 'FAILED', summary: 'too close', violations: {} });
    proposePlan.mockResolvedValue(GOOD);
    const { tree } = await ask();
    expect(button(tree, 'Apply edit').props.disabled).toBe(true);
    button(tree, 'Apply edit').props.onClick();
    expect(state.execute).not.toHaveBeenCalled();
  });

  it('is refused at Apply if the scenes changed after the draft was made', async () => {
    proposePlan.mockResolvedValue(GOOD);
    await ask();
    const renamed = { ...state.doc, scenes: state.doc.scenes.map((s) => (s.id === 'a' ? { ...s, name: 'X' } : s)) };
    const { tree } = render({ doc: renamed });
    expect(button(tree, 'Apply edit').props.disabled).toBe(true);
    expect(textOf(tree)).toMatch(/stale: scenes changed/);
    button(tree, 'Apply edit').props.onClick();
    expect(state.execute).not.toHaveBeenCalled();
  });

  it('the document used to compile is the one at the time of the request', async () => {
    let finish;
    proposePlan.mockReturnValue(new Promise((r) => { finish = r; }));
    render().intent.setText(SENTENCE);
    const pending = render().intent.plan();
    // The scenes change while the model is thinking.
    const renamed = { ...state.doc, scenes: state.doc.scenes.map((s) => (s.id === 'a' ? { ...s, name: 'X' } : s)) };
    render({ doc: renamed });
    finish(GOOD);
    await pending;
    const { intent, tree } = render({ doc: renamed });
    expect(intent.proposal).toBeNull();
    expect(textOf(tree)).toMatch(/stale: scenes changed/);
    expect(checkProposal).not.toHaveBeenCalled();
  });
});

describe('robustness', () => {
  it('tells the operator what the planner left out of a partly supported request', async () => {
    proposePlan.mockResolvedValue(ok([{ op: 'patch', scene: 1, scale: 2 }], ['turn the drones red']));
    const { tree } = await ask();
    expect(findAll(tree, (n) => n.props['data-testid'] === 'edit-unsupported')).toHaveLength(1);
    expect(textOf(tree)).toContain('turn the drones red');
    expect(textOf(tree)).toContain('Planner: Edit scene 1');
  });

  it('planning is not left stuck when something unexpected throws', async () => {
    proposePlan.mockResolvedValue({ status: 'ok', get ops() { throw new Error('boom'); } });
    const { tree, intent } = await ask();
    expect(intent.planning).toBe(false);
    expect(textOf(tree)).toContain('The planner failed unexpectedly: boom');
  });

  it('a status that is not a known one (even an inherited name) is treated as a refusal, not a message lookup', async () => {
    proposePlan.mockResolvedValue({ status: 'constructor', error: 'weird' });
    const { tree } = await ask();
    expect(proposePlan).toHaveBeenCalledTimes(2);
    expect(textOf(tree)).toContain("The planner's draft was refused: weird");
  });
});

describe('the one repair', () => {
  it('an out-of-range answer is repaired once, with the exact refusal reason as the note', async () => {
    proposePlan.mockResolvedValueOnce(TOO_BIG).mockResolvedValueOnce(GOOD);
    const { intent } = await ask();
    expect(proposePlan).toHaveBeenCalledTimes(2);
    expect(proposePlan.mock.calls[0][0].repairNote).toBeNull();
    expect(proposePlan.mock.calls[1][0].repairNote).toBe('step 1: scale 9 is outside the allowed range 0.25 to 3');
    expect(intent.proposal).not.toBeNull();
  });

  it('still out of range after the repair: stops, shows the reason, no proposal, never a third call', async () => {
    proposePlan.mockResolvedValue(TOO_BIG);
    const { tree, intent } = await ask();
    expect(proposePlan).toHaveBeenCalledTimes(2);
    expect(intent.proposal).toBeNull();
    expect(checkProposal).not.toHaveBeenCalled();
    expect(textOf(tree)).toContain("The planner's draft was refused: step 1: scale 9 is outside the allowed range 0.25 to 3");
    expect(findAll(tree, (n) => n.type === 'button' && textOf(n).includes('Apply edit'))).toHaveLength(0);
  });

  it('a schema_error is repaired once with the backend reason, then reported', async () => {
    proposePlan.mockResolvedValue({ status: 'schema_error', error: 'op 1: scene must be 1 to 2' });
    const { tree } = await ask();
    expect(proposePlan).toHaveBeenCalledTimes(2);
    expect(proposePlan.mock.calls[1][0].repairNote).toBe('op 1: scene must be 1 to 2');
    expect(textOf(tree)).toContain("The planner's draft was refused: op 1: scene must be 1 to 2");
  });

  it('a compile failure that the repair fixes shows the fixed proposal and no refusal', async () => {
    proposePlan.mockResolvedValueOnce(ok([{ op: 'patch', scene: 7, scale: 2 }])).mockResolvedValueOnce(GOOD);
    const { tree } = await ask();
    expect(proposePlan.mock.calls[1][0].repairNote).toMatch(/scene 7 does not exist \(the show has 2\)/);
    expect(textOf(tree)).not.toContain('refused');
    expect(textOf(tree)).toContain('PASSED');
  });
});

describe('when the model cannot help', () => {
  it.each([
    ['unavailable', /Ollama is not reachable/],
    ['timeout', /took too long/],
  ])('%s: a plain message, one call, no proposal, no repair', async (status, message) => {
    proposePlan.mockResolvedValue({ status });
    const { tree, intent } = await ask();
    expect(proposePlan).toHaveBeenCalledTimes(1);
    expect(intent.proposal).toBeNull();
    expect(textOf(tree)).toMatch(message);
    expect(button(tree, 'Ask planner').props.disabled).toBe(false);
  });

  it('a rejected request (network or 422) is reported, not thrown', async () => {
    proposePlan.mockRejectedValue(new Error('Failed to fetch'));
    const { tree } = await ask();
    expect(textOf(tree)).toContain('The planner request failed: Failed to fetch');
    expect(proposePlan).toHaveBeenCalledTimes(1);
  });

  it('an unsupported request (no ops) says so and is not repaired', async () => {
    proposePlan.mockResolvedValue(ok([], ['show length']));
    const { tree, intent } = await ask();
    expect(proposePlan).toHaveBeenCalledTimes(1);
    expect(intent.proposal).toBeNull();
    expect(textOf(tree)).toMatch(/not supported: show length/);
  });

  it('the typed Preview edit still works with the planner offline', async () => {
    proposePlan.mockResolvedValue({ status: 'unavailable' });
    render().intent.setText('set hold of scene 1 to 8 s');
    await render().intent.preview();
    expect(render().intent.proposal.summary).toContain('Set hold of scene 1');
    expect(proposePlan).not.toHaveBeenCalled();
  });
});

describe('latest request wins', () => {
  it('a dismiss while the model is working drops its answer', async () => {
    let finish;
    proposePlan.mockReturnValue(new Promise((r) => { finish = r; }));
    render().intent.setText(SENTENCE);
    const pending = render().intent.plan();
    render().intent.dismiss();
    finish(GOOD);
    await pending;
    const { intent } = render();
    expect(intent.proposal).toBeNull();
    expect(intent.planning).toBe(false);
    expect(checkProposal).not.toHaveBeenCalled();
  });

  it('an older answer cannot overwrite a newer request', async () => {
    let finishOld;
    proposePlan.mockReturnValueOnce(new Promise((r) => { finishOld = r; }));
    render().intent.setText(SENTENCE);
    const first = render().intent.plan();

    proposePlan.mockResolvedValueOnce(ok([{ op: 'patch', scene: 1, hold_seconds: 9 }]));
    await render().intent.plan();
    finishOld(GOOD);
    await first;

    const { tree } = render();
    expect(textOf(tree)).toContain('Edit scene 1 ("One")');
    expect(textOf(tree)).not.toContain('Edit scene 2');
    expect(checkProposal).toHaveBeenCalledTimes(1);
  });

  it('a typed preview during planning takes over and the model answer is dropped', async () => {
    let finish;
    proposePlan.mockReturnValue(new Promise((r) => { finish = r; }));
    render().intent.setText(SENTENCE);
    const pending = render().intent.plan();
    render().intent.setText('set hold of scene 1 to 8 s');
    await render().intent.preview();
    finish(GOOD);
    await pending;
    const { intent } = render();
    expect(intent.planning).toBe(false);
    expect(intent.proposal.summary).toContain('Set hold of scene 1');
  });
});
