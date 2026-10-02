import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import VaultPage from './VaultPage.jsx';
import { addVersion } from '../../engine/projectVersions.js';

const noop = () => {};
const base = { id: 'p1', name: 'Diwali', scenes: [{ id: 's1' }], rate: 4, dronesList: [{ id: 0 }], choreoData: null };
const render = (projectsList) => renderToStaticMarkup(
  <VaultPage
    projectsList={projectsList} activeProjectId="p1"
    onLoad={noop} onCreate={noop} onDelete={noop} onRestore={noop}
  />,
);

describe('VaultPage save history', () => {
  it('shows no history for a project that has never been versioned', () => {
    expect(render([base])).not.toContain('Save history');
  });

  it('lists every saved version, newest first, each with a restore action', () => {
    let proj = addVersion(base, base, Date.UTC(2026, 9, 3, 10, 0, 0));
    const edited = { ...base, scenes: [{ id: 's1' }, { id: 's2' }, { id: 's3' }] };
    proj = addVersion({ ...proj, ...edited }, edited, Date.UTC(2026, 9, 3, 11, 0, 0));

    const html = render([proj]);
    expect(html).toContain('Save history (2)');
    const rows = [...html.matchAll(/<li>.*?<\/li>/g)].map((m) => m[0]);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toContain('3 scenes');
    expect(rows[1]).toContain('1 scenes');
    for (const row of rows) expect(row).toMatch(/aria-label="Restore Diwali to [^"]+"/);
  });
});
