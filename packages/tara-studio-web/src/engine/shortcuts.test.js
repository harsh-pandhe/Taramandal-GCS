import { describe, it, expect } from 'vitest';
import { historyShortcut, cameraShortcut } from './shortcuts.js';

const key = (k, mods = {}, target = { tagName: 'BODY' }) => ({ key: k, target, ...mods });

describe('historyShortcut', () => {
  it('maps Ctrl+Z and Cmd+Z to undo', () => {
    expect(historyShortcut(key('z', { ctrlKey: true }))).toBe('undo');
    expect(historyShortcut(key('z', { metaKey: true }))).toBe('undo');
  });

  it('maps Ctrl+Shift+Z and Cmd+Shift+Z to redo', () => {
    // With Shift held the browser reports an uppercase key.
    expect(historyShortcut(key('Z', { ctrlKey: true, shiftKey: true }))).toBe('redo');
    expect(historyShortcut(key('Z', { metaKey: true, shiftKey: true }))).toBe('redo');
  });

  it('maps Ctrl+Y to redo, but not Cmd+Y', () => {
    expect(historyShortcut(key('y', { ctrlKey: true }))).toBe('redo');
    expect(historyShortcut(key('y', { metaKey: true }))).toBeNull();
  });

  it('ignores plain keys, Alt combinations and other letters', () => {
    expect(historyShortcut(key('z'))).toBeNull();
    expect(historyShortcut(key('z', { ctrlKey: true, altKey: true }))).toBeNull();
    expect(historyShortcut(key('s', { ctrlKey: true }))).toBeNull();
    expect(historyShortcut(null)).toBeNull();
  });

  it('leaves Ctrl+Z to the browser while typing in a text field', () => {
    for (const target of [
      { tagName: 'INPUT', type: 'text' },
      { tagName: 'INPUT', type: 'number' },
      { tagName: 'INPUT' },
      { tagName: 'TEXTAREA' },
      { tagName: 'SELECT' },
      { tagName: 'DIV', isContentEditable: true },
    ]) {
      expect(historyShortcut(key('z', { ctrlKey: true }, target))).toBeNull();
    }
  });

  it('still works when a slider, checkbox or colour picker has focus', () => {
    for (const type of ['range', 'checkbox', 'color']) {
      expect(historyShortcut(key('z', { ctrlKey: true }, { tagName: 'INPUT', type }))).toBe('undo');
    }
  });
});

describe('cameraShortcut', () => {
  it('maps F to fit and Home to reset', () => {
    expect(cameraShortcut(key('f'))).toBe('fit');
    expect(cameraShortcut(key('F'))).toBe('fit'); // Caps Lock
    expect(cameraShortcut(key('Home'))).toBe('reset');
  });

  it('ignores modified keys, so Ctrl+F stays the browser search', () => {
    for (const mod of ['ctrlKey', 'metaKey', 'altKey', 'shiftKey']) {
      expect(cameraShortcut(key('f', { [mod]: true }))).toBeNull();
      expect(cameraShortcut(key('Home', { [mod]: true }))).toBeNull();
    }
  });

  it('ignores other keys and a missing event', () => {
    expect(cameraShortcut(key('g'))).toBeNull();
    expect(cameraShortcut(key('End'))).toBeNull();
    expect(cameraShortcut(null)).toBeNull();
  });

  it('leaves the keys alone while any form control has focus', () => {
    for (const target of [
      { tagName: 'INPUT', type: 'text' },
      { tagName: 'INPUT', type: 'range' }, // Home moves a slider to its minimum
      { tagName: 'TEXTAREA' },
      { tagName: 'SELECT' },
      { tagName: 'DIV', isContentEditable: true },
    ]) {
      expect(cameraShortcut(key('f', {}, target))).toBeNull();
      expect(cameraShortcut(key('Home', {}, target))).toBeNull();
    }
  });

  it('works when a button has focus, as after clicking a view tab', () => {
    expect(cameraShortcut(key('f', {}, { tagName: 'BUTTON' }))).toBe('fit');
  });
});
