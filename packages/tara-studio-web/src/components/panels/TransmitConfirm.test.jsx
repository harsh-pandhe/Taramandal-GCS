import { describe, expect, it, vi } from 'vitest';
import { button, findAll, makeStateStub } from '../../testing/elementTree.js';

// No DOM here (see ShapeLibraryModal.test.jsx): useState is stubbed so the
// panel can be called as a function and its click handlers driven directly.
const stub = makeStateStub();
vi.mock('react', async (orig) => ({ ...(await orig()), useState: (i) => stub.useState(i), useEffect: () => {} }));
const { default: TransmitPanel } = await import('./TransmitPanel.jsx');
const { transmitMessage } = await import('../../engine/confirmations.js');
const { default: ConfirmDialog } = await import('../modals/ConfirmDialog.jsx');

const PASSED = { status: 'PASSED', violations: {}, feasibility_certified: true };
const render = (over = {}) => {
  stub.i = 0;
  return TransmitPanel({
    homeLat: 0, homeLon: 0, onHomeLat() {}, onHomeLon() {}, onExport() {}, onExportPreview() {},
    onSendToGCS: vi.fn(), sending: false, hasShow: true, validation: PASSED, validating: false,
    droneCount: 24, ...over,
  });
};
const dialog = (tree) => findAll(tree, (n) => n.type === ConfirmDialog)[0];
const TRANSMIT = 'Transmit to GCS Fleet';

describe('Transmit to GCS asks first (issue #92)', () => {
  it('clicking Transmit opens the dialog and does not send', () => {
    stub.slots = [];
    const onSendToGCS = vi.fn();
    button(render({ onSendToGCS }), TRANSMIT).props.onClick();
    expect(onSendToGCS).not.toHaveBeenCalled();
    expect(dialog(render({ onSendToGCS }))).toBeDefined();
  });

  it('Cancel closes it without sending', () => {
    stub.slots = [];
    const onSendToGCS = vi.fn();
    button(render({ onSendToGCS }), TRANSMIT).props.onClick();
    dialog(render({ onSendToGCS })).props.onCancel();
    expect(onSendToGCS).not.toHaveBeenCalled();
    expect(dialog(render({ onSendToGCS }))).toBeUndefined();
  });

  it('Confirm sends exactly once and closes the dialog', () => {
    stub.slots = [];
    const onSendToGCS = vi.fn();
    button(render({ onSendToGCS }), TRANSMIT).props.onClick();
    dialog(render({ onSendToGCS })).props.onConfirm();
    expect(onSendToGCS).toHaveBeenCalledTimes(1);
    expect(dialog(render({ onSendToGCS }))).toBeUndefined();
  });

  it('keeps the button gating as it was', () => {
    stub.slots = [];
    const gate = (over) => findAll(render(over), (n) => String(n.props.className).includes('btn-gcs'))[0].props.disabled;
    expect(gate({})).toBe(false);
    expect(gate({ sending: true })).toBe(true);
    expect(gate({ validation: { ...PASSED, status: 'FAILED' } })).toBe(true);
    expect(gate({ hasShow: false })).toBe(true);
  });

  it('says how many drones, that it goes to the GCS fleet, and that launch checks still run', () => {
    expect(transmitMessage(24)).toContain('24 drones');
    expect(transmitMessage(1)).toContain('1 drone ');
    expect(transmitMessage(24)).toMatch(/GCS fleet/);
    expect(transmitMessage(24)).toMatch(/Launch checks still run on the GCS dashboard/);
  });
});
