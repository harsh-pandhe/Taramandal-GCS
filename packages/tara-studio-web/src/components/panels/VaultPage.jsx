import { Plus, Trash, CheckCircle, AlertTriangle } from 'lucide-react';

/** Saved projects, persisted to localStorage. */
export default function VaultPage({
  projectsList, activeProjectId, onLoad, onCreate, onDelete,
}) {
  return (
    <main className="studio-viewport-page">
      <div className="page-header">
        <div>
          <h2>Project Vault</h2>
          <p>Save progress, switch workspaces, or reload a stored choreography.</p>
        </div>
        <button type="button" className="btn-primary btn-inline" onClick={onCreate}>
          <Plus size={15} /> New Show
        </button>
      </div>

      <div className="vault-grid">
        {projectsList.map((proj) => (
          <div
            className={`vault-card${proj.id === activeProjectId ? ' active' : ''}`}
            key={proj.id}
            onClick={() => onLoad(proj.id)}
          >
            <h3 className="vault-proj-name">{proj.name}</h3>
            <div className="vault-proj-meta">
              <span>Scenes: <strong>{proj.scenes?.length ?? 0}</strong></span>
              <span>Vehicles: <strong>{proj.dronesList?.length ?? 0}</strong></span>
              <span>Rate: <strong>{proj.rate}Hz</strong></span>
            </div>

            <div className="vault-proj-status">
              {proj.choreoData ? (
                <span className="status-badge valid"><CheckCircle size={12} /> Trajectory built</span>
              ) : (
                <span className="status-badge empty"><AlertTriangle size={12} /> Not built</span>
              )}
            </div>

            <div className="vault-card-actions">
              <button type="button" className="btn-primary btn-sm" onClick={(e) => { e.stopPropagation(); onLoad(proj.id); }}>
                Load
              </button>
              <button
                type="button" className="btn-danger-outline btn-sm"
                onClick={(e) => { e.stopPropagation(); onDelete(proj.id); }}
                disabled={projectsList.length <= 1}
                aria-label={`Delete ${proj.name}`}
              >
                <Trash size={14} />
              </button>
            </div>
          </div>
        ))}
      </div>
    </main>
  );
}
