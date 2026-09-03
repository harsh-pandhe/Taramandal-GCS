import { Compass, FolderOpen, Activity, Save, Edit2 } from 'lucide-react';

const PAGES = [
  { id: 'choreo', icon: Compass, label: 'Sequencer' },
  { id: 'projects', icon: FolderOpen, label: 'Project Vault' },
  { id: 'fleet', icon: Activity, label: 'Swarm Fleet' },
];

/** Brand, page navigation, and the active project's name/save control. */
export default function Sidebar({
  activePage, onNavigate, projectName, onProjectName, onSave,
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
        <span className="ui-field-label">Active Project</span>
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
        <button type="button" className="btn-secondary btn-inline" onClick={onSave}>
          <Save size={14} /> Save Project
        </button>
      </div>
    </aside>
  );
}
