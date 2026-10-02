import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import HistoryBar from './HistoryBar.jsx';

// Static markup, as in SafetyPanel.test.jsx: the bar is pure presentation.
const noop = () => {};
const render = (over) => renderToStaticMarkup(
  <HistoryBar
    history={{
      canUndo: false, canRedo: false, undo: noop, redo: noop, jumpTo: noop,
      entries: [], applied: 0, ...over,
    }}
  />,
);
const button = (html, label) => html.match(new RegExp(`<button[^>]*aria-label="${label}"[^>]*>`))[0];

describe('HistoryBar', () => {
  it('disables both buttons when there is no history', () => {
    const html = render();
    expect(button(html, 'Undo')).toContain('disabled');
    expect(button(html, 'Redo')).toContain('disabled');
    expect(html).toContain('0/0');
  });

  it('enables each button only when that direction is available', () => {
    const html = render({ canUndo: true, canRedo: false });
    expect(button(html, 'Undo')).not.toContain('disabled');
    expect(button(html, 'Redo')).toContain('disabled');
  });

  it('lists every edit, marks the current one and dims the undone ones', () => {
    const html = render({
      canUndo: true, canRedo: true, applied: 2,
      entries: [{ label: 'Add scene' }, { label: 'Edit scene name' }, { label: 'Delete scene "B"' }],
    });
    expect(html).toContain('2/3');
    const items = [...html.matchAll(/<li><button[^>]*class="(\w*)"[^>]*>([^<]*)</g)].map((m) => [m[2], m[1]]);
    expect(items).toEqual([
      ['Opened', ''],
      ['Add scene', ''],
      ['Edit scene name', 'current'],
      ['Delete scene &quot;B&quot;', 'undone'],
    ]);
  });

  it('marks "Opened" as current once everything is undone', () => {
    const html = render({ canRedo: true, applied: 0, entries: [{ label: 'Add scene' }] });
    expect(html).toMatch(/<button[^>]*class="current"[^>]*>Opened</);
  });
});
