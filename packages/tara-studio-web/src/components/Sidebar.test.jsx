import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import Sidebar from './Sidebar.jsx';

const noop = () => {};

const render = (props = {}) => renderToStaticMarkup(
  <Sidebar
    activePage="choreo"
    onNavigate={noop}
    projectName="Dandiya Show"
    onProjectName={noop}
    onSave={noop}
    vaultError={null}
    isDirty={false}
    {...props}
  />,
);

describe('Sidebar unsaved-changes indicator (issue #132)', () => {
  it('shows no "Unsaved" badge and the neutral Save style when the project is clean', () => {
    const html = render({ isDirty: false });
    expect(html).not.toContain('Unsaved');
    expect(html).toContain('Save Project');
    expect(html).toContain('btn-secondary');
    expect(html).not.toContain('btn-primary');
  });

  it('shows the "Unsaved" badge and switches Save to the primary style when dirty', () => {
    const html = render({ isDirty: true });
    expect(html).toContain('Unsaved');
    // The label stays "Save Project" - a stable accessible name regardless
    // of dirty state, so e2e specs can always find it by that role name.
    expect(html).toContain('Save Project');
    expect(html).toContain('btn-primary');
    expect(html).not.toContain('btn-secondary');
  });

  it('always renders the project name and a working save control regardless of dirty state', () => {
    for (const isDirty of [false, true]) {
      const html = render({ isDirty, projectName: 'Diya Flame' });
      expect(html).toContain('Diya Flame');
      expect(html).toContain('aria-label="Project name"');
    }
  });

  it('still shows a vault error alongside the dirty indicator', () => {
    const html = render({ isDirty: true, vaultError: 'Could not reach the Project Vault.' });
    expect(html).toContain('Unsaved');
    expect(html).toContain('Could not reach the Project Vault.');
  });
});
