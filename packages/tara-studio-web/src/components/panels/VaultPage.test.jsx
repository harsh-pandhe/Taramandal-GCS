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

// Issue #101: the projects come from the backend's vault, which can be
// unreachable, and the list no longer carries each project's built show.
describe('VaultPage backed by the Project Vault', () => {
  const page = (props) => renderToStaticMarkup(
    <VaultPage
      projectsList={[base]} activeProjectId="p1"
      onLoad={noop} onCreate={noop} onDelete={noop} onRestore={noop} onRetry={noop}
      {...props}
    />,
  );

  it('shows a project as built from the vault\'s flag, without its trajectory loaded', () => {
    expect(page({ projectsList: [{ ...base, hasShow: true }] })).toContain('Trajectory built');
    expect(page({ projectsList: [{ ...base, hasShow: false }] })).toContain('Not built');
  });

  it('says the vault is unreachable, that nothing is lost, and offers a retry', () => {
    const html = page({ projectsList: [], status: 'offline', error: 'The Project Vault cannot be reached.' });
    expect(html).toContain('role="alert"');
    expect(html).toContain('The Project Vault cannot be reached.');
    expect(html).toContain('not lost');
    expect(html).toMatch(/<button[^>]*>Retry<\/button>/);
    // A new show cannot be created in a vault that is not there.
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>.*New Show/);
  });

  it('shows a vault notice while staying usable', () => {
    const html = page({ error: 'Your edits were saved as a separate project.' });
    expect(html).toContain('Your edits were saved as a separate project.');
    expect(html).not.toContain('Retry');
    expect(html).not.toMatch(/<button[^>]*disabled=""[^>]*>.*New Show/);
  });

  it('shows nothing extra when the vault is fine', () => {
    const html = page({});
    expect(html).not.toContain('role="alert"');
    expect(html).not.toContain('Opening the Project Vault');
  });
});
