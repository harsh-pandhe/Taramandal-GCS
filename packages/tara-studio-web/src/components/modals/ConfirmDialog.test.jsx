import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { button, findAll } from '../../testing/elementTree.js';

// No DOM here (see ShapeLibraryModal.test.jsx): hooks are stubbed so the
// component can be called as a function and its handlers driven directly.
// Focus (Cancel first, trap, return) is checked end to end in e2e.
vi.mock('react', async (orig) => ({
  ...(await orig()),
  useRef: () => ({ current: null }),
  useEffect: () => {},
  useId: () => 'cd',
}));
const { default: ConfirmDialog } = await import('./ConfirmDialog.jsx');
const { dialogKeyAction } = await import('../../engine/confirmations.js');

const props = (over = {}) => ({
  title: 'Delete project', message: 'It cannot be undone.', confirmLabel: 'Delete project',
  onConfirm: vi.fn(), onCancel: vi.fn(), ...over,
});
const root = (p) => ConfirmDialog(p);
const card = (tree) => findAll(tree, (n) => n.props.role === 'alertdialog')[0];
const event = (over = {}) => ({ key: '', shiftKey: false, preventDefault: vi.fn(), stopPropagation: vi.fn(), ...over });

describe('ConfirmDialog markup', () => {
  const html = renderToStaticMarkup(<ConfirmDialog {...props({ danger: true })} />);

  it('is a modal alertdialog labelled by its title and described by its message', () => {
    expect(html).toContain('role="alertdialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toMatch(/aria-labelledby="[^"]+-title"/);
    expect(html).toMatch(/aria-describedby="[^"]+-message"/);
    expect(html).toMatch(/<h3 id="[^"]+-title">Delete project<\/h3>/);
    expect(html).toMatch(/<p id="[^"]+-message"[^>]*>It cannot be undone\.<\/p>/);
  });

  it('puts Cancel before the confirm button, which is named for the action', () => {
    expect(html.indexOf('>Cancel<')).toBeGreaterThan(-1);
    expect(html.indexOf('>Cancel<')).toBeLessThan(html.indexOf('>Delete project</button>'));
    expect(html).not.toContain('>OK<');
  });

  it('styles a dangerous confirm as danger, an ordinary one as primary', () => {
    expect(html).toContain('btn-danger-solid');
    expect(renderToStaticMarkup(<ConfirmDialog {...props()} />)).toContain('btn-primary');
  });

  it('lets the caller rename Cancel', () => {
    expect(renderToStaticMarkup(<ConfirmDialog {...props({ cancelLabel: 'Keep it' })} />)).toContain('>Keep it<');
  });
});

describe('ConfirmDialog behaviour', () => {
  it('confirm calls onConfirm exactly once and never onCancel', () => {
    const p = props();
    button(root(p), 'Delete project').props.onClick();
    expect(p.onConfirm).toHaveBeenCalledTimes(1);
    expect(p.onCancel).not.toHaveBeenCalled();
  });

  it('the Cancel button cancels without confirming', () => {
    const p = props();
    button(root(p), 'Cancel').props.onClick();
    expect(p.onCancel).toHaveBeenCalledTimes(1);
    expect(p.onConfirm).not.toHaveBeenCalled();
  });

  it('Esc cancels without confirming', () => {
    const p = props();
    const e = event({ key: 'Escape' });
    root(p).props.onKeyDown(e);
    expect(p.onCancel).toHaveBeenCalledTimes(1);
    expect(p.onConfirm).not.toHaveBeenCalled();
    expect(e.preventDefault).toHaveBeenCalled();
  });

  it('Enter and other keys do nothing at the dialog level', () => {
    const p = props();
    root(p).props.onKeyDown(event({ key: 'Enter' }));
    expect(p.onCancel).not.toHaveBeenCalled();
    expect(p.onConfirm).not.toHaveBeenCalled();
  });

  it('a click on the backdrop cancels without confirming; a click on the card does not', () => {
    const p = props();
    const tree = root(p);
    tree.props.onPointerDown();
    expect(p.onCancel).toHaveBeenCalledTimes(1);
    expect(p.onConfirm).not.toHaveBeenCalled();
    const stop = vi.fn();
    card(tree).props.onPointerDown({ stopPropagation: stop });
    expect(stop).toHaveBeenCalled();
    expect(p.onCancel).toHaveBeenCalledTimes(1);
  });
});

describe('dialogKeyAction (focus trap)', () => {
  it('Tab on the last control wraps to the first, Shift+Tab on the first wraps to the last', () => {
    expect(dialogKeyAction({ key: 'Tab' }, 1, 2)).toEqual({ type: 'focus', index: 0 });
    expect(dialogKeyAction({ key: 'Tab', shiftKey: true }, 0, 2)).toEqual({ type: 'focus', index: 1 });
  });
  it('Tab inside moves on by one; Tab from outside enters at the first control', () => {
    expect(dialogKeyAction({ key: 'Tab' }, 0, 2)).toEqual({ type: 'focus', index: 1 });
    expect(dialogKeyAction({ key: 'Tab' }, -1, 2)).toEqual({ type: 'focus', index: 0 });
  });
  it('Esc is a cancel and other keys are ignored', () => {
    expect(dialogKeyAction({ key: 'Escape' }, 0, 2)).toEqual({ type: 'cancel' });
    expect(dialogKeyAction({ key: 'a' }, 0, 2)).toBeNull();
  });
});
