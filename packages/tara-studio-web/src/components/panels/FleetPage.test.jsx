import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import FleetPage from './FleetPage.jsx';
import { defaultFleet } from '../../engine/fleetCommands.js';

// Static markup, like SafetyPanel.test.jsx: checks what the operator gets in
// the DOM without needing a browser.
const noop = () => {};
const render = (n) => renderToStaticMarkup(
  <FleetPage
    dronesList={defaultFleet(n)}
    onAdd={noop} onRemove={noop} onUpdate={noop} onBulkUpdate={noop} onSetCount={noop}
  />,
);
const count = (html, re) => (html.match(re) || []).length;

describe('FleetPage', () => {
  it('renders a small fleet in full', () => {
    const html = render(6);
    expect(count(html, /role="row" aria-rowindex/g)).toBe(6);
    expect(html).toContain('6 vehicles');
  });

  it('keeps the DOM bounded at 100 drones: ~20 rows and colour pickers, not 100', () => {
    const html = render(100);
    const rows = count(html, /role="row" aria-rowindex/g);
    expect(rows).toBeGreaterThan(10);
    expect(rows).toBeLessThanOrEqual(22);
    // One colour picker per mounted row, plus the bulk one.
    expect(count(html, /type="color"/g)).toBe(rows + 1);
    expect(html).toContain('aria-rowcount="100"');
    expect(html).toContain('100 vehicles');
  });

  it('starts with nothing selected and the bulk actions disabled', () => {
    const html = render(100);
    expect(html).toContain('0 selected');
    const bulk = html.slice(html.indexOf('Set for selected'), html.indexOf('role="table"'));
    expect(count(bulk, /<button[^>]*disabled/g)).toBe(4);
  });
});
