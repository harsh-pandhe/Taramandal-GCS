import { describe, expect, it, vi, beforeEach } from 'vitest';
import { button, findAll, textOf, makeStateStub } from '../../testing/elementTree.js';
import { makeScene } from '../../engine/sequencer.js';

// No DOM here (see TransmitConfirm.test.jsx): the real hook runs with useState
// and useRef stubbed, so the panel it feeds can be rendered and clicked directly.
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

const { default: intentHook } = await import('../../hooks/useIntentProposal.js');
const { default: ProposalPanel } = await import('./ProposalPanel.jsx');

const mkDoc = () => ({
  scenes: [makeScene({ id: 'a', name: 'One' }), makeScene({ id: 'b', name: 'Two' })],
  dronesList: [],
});
const SENTENCE = 'set hold of scene 1 to 8 s';

const state = { doc: mkDoc(), execute: vi.fn(), numDrones: 6, rate: 4, pacing: 'standard' };
const render = (over = {}) => {
  stub.i = 0;
  const intent = intentHook({ ...state, ...over });
  return { intent, tree: ProposalPanel({ intent }) };
};
const applyButton = (tree) => button(tree, 'Apply edit');

// Types the sentence, previews it, and lets the (mocked) safety check settle.
async function previewWith(report, over = {}) {
  checkProposal.mockResolvedValue({ summary: `${report.status} summary`, violations: {}, ...report });
  render(over).intent.setText(SENTENCE);
  await render(over).intent.preview();
  return render(over);
}

beforeEach(() => {
  stub.slots = [];
  state.doc = mkDoc();
  state.execute = vi.fn();
  checkProposal.mockReset();
});

describe('Apply edit', () => {
  it('is enabled only for PASSED, and one click applies the edit once', async () => {
    const { tree } = await previewWith({ status: 'PASSED' });
    expect(applyButton(tree).props.disabled).toBe(false);

    applyButton(tree).props.onClick();
    expect(state.execute).toHaveBeenCalledTimes(1);
    const command = state.execute.mock.calls[0][0];
    expect(command.type).toBe('scene/batch');
    expect(command.mergeKey).toBeUndefined();
    // The dry run never executed anything itself.
    expect(render().intent.proposal).toBeNull();
  });

  it.each(['FAILED', 'UNVERIFIED', 'REFUSED'])('is disabled for %s, and a click does nothing', async (status) => {
    const { tree } = await previewWith({ status });
    expect(applyButton(tree).props.disabled).toBe(true);
    applyButton(tree).props.onClick();
    expect(state.execute).not.toHaveBeenCalled();
  });

  it('is disabled while the check is still running', async () => {
    let finish;
    checkProposal.mockReturnValue(new Promise((r) => { finish = r; }));
    render().intent.setText(SENTENCE);
    const pending = render().intent.preview();
    const { tree } = render();
    expect(applyButton(tree).props.disabled).toBe(true);
    expect(textOf(tree)).toContain('Checking safety');
    finish({ status: 'PASSED', summary: 'ok', violations: {} });
    await pending;
    expect(applyButton(render().tree).props.disabled).toBe(false);
  });

  it('is disabled and explains why when the scenes changed after the check passed', async () => {
    await previewWith({ status: 'PASSED' });
    const renamed = { ...state.doc, scenes: state.doc.scenes.map((s) => (s.id === 'b' ? { ...s, name: 'Renamed' } : s)) };
    const { tree } = render({ doc: renamed });
    expect(applyButton(tree).props.disabled).toBe(true);
    expect(textOf(tree)).toMatch(/stale: scenes changed/);

    // Even if a click got through, the handler checks again and does not apply.
    applyButton(tree).props.onClick();
    expect(state.execute).not.toHaveBeenCalled();
  });

  it('re-checks inside the click handler against the document as it is then', async () => {
    const { tree: old } = await previewWith({ status: 'PASSED' });
    expect(applyButton(old).props.disabled).toBe(false);
    // The scenes change; the handler of the older render is clicked before a re-render.
    state.doc = { ...state.doc, scenes: state.doc.scenes.slice().reverse() };
    render();
    applyButton(old).props.onClick();
    expect(state.execute).not.toHaveBeenCalled();
  });

  it('is disabled when fleet size, rate or speed changed since the check', async () => {
    await previewWith({ status: 'PASSED' });
    const { tree } = render({ numDrones: 7 });
    expect(applyButton(tree).props.disabled).toBe(true);
    applyButton(tree).props.onClick();
    expect(state.execute).not.toHaveBeenCalled();
  });

  it('an older check that finishes late cannot enable Apply for a newer proposal', async () => {
    let finishOld;
    checkProposal.mockReturnValueOnce(new Promise((r) => { finishOld = r; }));
    render().intent.setText(SENTENCE);
    const first = render().intent.preview();

    checkProposal.mockResolvedValueOnce({ status: 'FAILED', summary: 'newer fails', violations: {} });
    render().intent.setText('set hold of scene 2 to 9 s');
    await render().intent.preview();
    finishOld({ status: 'PASSED', summary: 'older passes', violations: {} });
    await first;

    const { tree } = render();
    expect(applyButton(tree).props.disabled).toBe(true);
    expect(textOf(tree)).toContain('newer fails');
    expect(textOf(tree)).not.toContain('older passes');
  });
});

describe('the panel', () => {
  it('shows the summary, the field-by-field change and the status', async () => {
    const { tree } = await previewWith({ status: 'PASSED' });
    const text = textOf(tree);
    expect(text).toContain('Set hold of scene 1 ("One") to 8 s');
    expect(text).toContain('Scene 1 "One" hold');
    expect(text).toContain('6 s → 8 s');
    expect(text).toContain('PASSED: PASSED summary');
  });

  it('lists the violations when FAILED', async () => {
    const violations = { proximity: [{ time: 6.1, drones: [2, 3], distance: 1.2 }], velocity: [], acceleration: [] };
    const { tree } = await previewWith({ status: 'FAILED', violations });
    expect(textOf(tree)).toContain('Drones 2 & 3 1.2 m apart at t = 6.1 s');
  });

  it('a sentence the parser refuses shows its reason and no proposal', async () => {
    render().intent.setText('transmit the show');
    await render().intent.preview();
    const { tree, intent } = render();
    expect(intent.proposal).toBeNull();
    expect(textOf(tree)).toMatch(/not supported/);
    expect(findAll(tree, (n) => n.type === 'button' && textOf(n).includes('Apply edit'))).toHaveLength(0);
    expect(checkProposal).not.toHaveBeenCalled();
  });

  it('Dismiss clears the proposal without applying', async () => {
    const { tree } = await previewWith({ status: 'PASSED' });
    button(tree, 'Dismiss edit').props.onClick();
    expect(render().intent.proposal).toBeNull();
    expect(state.execute).not.toHaveBeenCalled();
  });

  it('uses labels and a placeholder distinct from the AI panel, and a plain text input', () => {
    const { tree } = render();
    const input = findAll(tree, (n) => n.type === 'input')[0];
    expect(input.props.type).toBe('text');
    expect(input.props.placeholder).toBe('make scene 2 a heart');
    expect(button(tree, 'Preview edit')).toBeDefined();
    const labels = findAll(tree, (n) => n.type === 'button').map(textOf).join('|');
    expect(labels).not.toMatch(/Propose|Discard/);
  });
});
