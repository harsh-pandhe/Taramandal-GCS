import { describe, expect, it, vi } from 'vitest';
import { button, findAll, textOf, makeStateStub } from '../../testing/elementTree.js';

// No DOM here (see ShapeLibraryModal.test.jsx): useState is stubbed so the
// page can be called as a function and its click handlers driven directly.
const stub = makeStateStub();
vi.mock('react', async (orig) => ({ ...(await orig()), useState: (i) => stub.useState(i) }));
const { default: VaultPage } = await import('./VaultPage.jsx');
const { confirmationFor } = await import('../../engine/confirmations.js');
const { default: ConfirmDialog } = await import('../modals/ConfirmDialog.jsx');

const base = { id: 'p1', name: 'Diwali', scenes: [], rate: 4, dronesList: [], choreoData: null };
const second = { ...base, id: 'p2', name: 'Holi' };

const draw = (over = {}) => {
  stub.i = 0;
  return VaultPage({
    projectsList: [base, second], activeProjectId: 'p1',
    onLoad: vi.fn(), onCreate: vi.fn(), onDelete: vi.fn(), onRestore: vi.fn(), isDirty: false, ...over,
  });
};
const dialogOf = (tree) => findAll(tree, (n) => n.type === ConfirmDialog)[0];
const del = (tree) => findAll(tree, (n) => n.props['aria-label'] === 'Delete Holi')[0];
const load = (tree) => findAll(tree, (n) => n.type === 'button' && textOf(n).trim() === 'Load')[1];
const click = (n) => n.props.onClick({ stopPropagation() {} });

describe('VaultPage confirmations (issue #92)', () => {
  it('delete asks first, names the project, and does not delete until confirmed', () => {
    stub.slots = [];
    const onDelete = vi.fn();
    click(del(draw({ onDelete })));
    expect(onDelete).not.toHaveBeenCalled();
    const d = dialogOf(draw({ onDelete }));
    expect(d.props.message).toContain('"Holi"');
    expect(d.props.message).toContain('cannot be undone');
    expect(d.props.confirmLabel).toBe('Delete project');
    d.props.onCancel();
    expect(onDelete).not.toHaveBeenCalled();
    expect(dialogOf(draw({ onDelete }))).toBeUndefined();
    click(del(draw({ onDelete })));
    dialogOf(draw({ onDelete })).props.onConfirm();
    expect(onDelete).toHaveBeenCalledTimes(1);
    expect(onDelete).toHaveBeenCalledWith('p2');
    expect(dialogOf(draw({ onDelete }))).toBeUndefined();
  });

  it('load on a clean editor runs at once with no dialog', () => {
    stub.slots = [];
    const onLoad = vi.fn();
    click(load(draw({ onLoad })));
    expect(onLoad).toHaveBeenCalledWith('p2');
    expect(dialogOf(draw({ onLoad }))).toBeUndefined();
  });

  it('load with unsaved changes asks, and waits for the confirm', () => {
    stub.slots = [];
    const onLoad = vi.fn();
    click(load(draw({ onLoad, isDirty: true })));
    expect(onLoad).not.toHaveBeenCalled();
    const d = dialogOf(draw({ onLoad, isDirty: true }));
    expect(d.props.message).toContain('unsaved changes');
    expect(d.props.confirmLabel).toBe('Discard changes and load');
    d.props.onCancel();
    expect(onLoad).not.toHaveBeenCalled();
    click(load(draw({ onLoad, isDirty: true })));
    dialogOf(draw({ onLoad, isDirty: true })).props.onConfirm();
    expect(onLoad).toHaveBeenCalledTimes(1);
    expect(onLoad).toHaveBeenCalledWith('p2');
  });

  it('a click on the project card follows the same rule as the Load button', () => {
    stub.slots = [];
    const onLoad = vi.fn();
    const cards = (over) => findAll(draw(over), (n) => /^vault-card( |$)/.test(n.props.className ?? ''));
    cards({ onLoad, isDirty: true })[1].props.onClick();
    expect(onLoad).not.toHaveBeenCalled();
    expect(dialogOf(draw({ onLoad, isDirty: true }))).toBeDefined();
  });

  it('New Show asks only when there are unsaved changes', () => {
    stub.slots = [];
    const onCreate = vi.fn();
    button(draw({ onCreate }), 'New Show').props.onClick();
    expect(onCreate).toHaveBeenCalledTimes(1);
    expect(dialogOf(draw({ onCreate }))).toBeUndefined();

    button(draw({ onCreate, isDirty: true }), 'New Show').props.onClick();
    expect(onCreate).toHaveBeenCalledTimes(1);
    dialogOf(draw({ onCreate, isDirty: true })).props.onConfirm();
    expect(onCreate).toHaveBeenCalledTimes(2);
  });

  it('confirmationFor: delete always asks, load and create only when dirty', () => {
    const ctx = (isDirty) => ({ isDirty, projectName: 'X' });
    expect(confirmationFor({ kind: 'delete' }, ctx(false))).not.toBeNull();
    expect(confirmationFor({ kind: 'load' }, ctx(false))).toBeNull();
    expect(confirmationFor({ kind: 'create' }, ctx(false))).toBeNull();
    expect(confirmationFor({ kind: 'load' }, ctx(true))).not.toBeNull();
    expect(confirmationFor({ kind: 'create' }, ctx(true))).not.toBeNull();
  });
});
