import { Compass, FolderOpen, Activity, Save, Edit2 } from 'lucide-react';
import { Badge } from './common/index.jsx';

const PAGES = [
  { id: 'choreo', icon: Compass, label: 'Sequencer' },
  { id: 'projects', icon: FolderOpen, label: 'Project Vault' },
  { id: 'fleet', icon: Activity, label: 'Swarm Fleet' },
];

/** Brand, page navigation, and the active project's name/save control. */
export default function Sidebar({
  activePage, onNavigate, projectName, onProjectName, onSave, vaultError, isDirty,
}) {
  return (
    <aside className="studio-sidebar">
      <div className="sidebar-brand">
        <h1 className="brand-name">TARAMANDAL</h1>
        <span className="brand-sub">STUDIO V2</span>
      </div>

      <nav className="sidebar-nav">
        {PAGES.map(({ id, icon: Icon, label }) => (
          <button
            key={id}
            type="button"
            className={`nav-btn${activePage === id ? ' active' : ''}`}
            onClick={() => onNavigate(id)}
          >
            <Icon size={16} /><span>{label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-project-info">
        <span className="ui-field-label">
          Active Project
          {isDirty && (
            <Badge tone="warn" title="This project has changes that haven't been saved yet">
              Unsaved
            </Badge>
          )}
        </span>
        <div className="project-name-display">
          <Edit2 size={12} />
          <input
            className="project-name-input"
            value={projectName}
            onChange={(e) => onProjectName(e.target.value)}
            onBlur={onSave}
            aria-label="Project name"
          />
        </div>
        {/* Issue #132: switches to the primary button style once there's
            something to save - the label itself stays "Save Project" so it
            keeps one stable accessible name regardless of dirty state. */}
        <button
          type="button"
          className={`btn-inline ${isDirty ? 'btn-primary' : 'btn-secondary'}`}
          onClick={onSave}
        >
          <Save size={14} /> Save Project
        </button>
        {vaultError && <p className="vault-error" role="alert">{vaultError}</p>}
      </div>
    </aside>
  );
}
