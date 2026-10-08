import { Plus, Trash, CheckCircle, AlertTriangle, History } from 'lucide-react';
import { versionsOf } from '../../engine/projectVersions.js';

const fmtSavedAt = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
};

/** Saved projects, kept in the backend's Project Vault (issue #101). */
export default function VaultPage({
  projectsList, activeProjectId, onLoad, onCreate, onDelete, onRestore,
  status = 'ready', error = null, onRetry,
}) {
  return (
    <main className="studio-viewport-page">
      <div className="page-header">
        <div>
          <h2>Project Vault</h2>
          <p>Save progress, switch workspaces, or reload a stored choreography.</p>
        </div>
        <button type="button" className="btn-primary btn-inline" onClick={onCreate} disabled={status !== 'ready'}>
          <Plus size={15} /> New Show
        </button>
      </div>

      {status === 'loading' && <p className="vault-note">Opening the Project Vault…</p>}
      {status === 'offline' && (
        <div className="vault-alert" role="alert">
          <AlertTriangle size={15} />
          <span>
            {error ?? 'The Project Vault cannot be reached.'} The show on screen is not lost: Save
            Project stores it once the vault is back.
          </span>
          <button type="button" className="btn-secondary btn-sm" onClick={onRetry}>Retry</button>
        </div>
      )}
      {status === 'ready' && error && (
        <div className="vault-alert" role="alert"><AlertTriangle size={15} /><span>{error}</span></div>
      )}

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
              {proj.hasShow || proj.choreoData ? (
                <span className="status-badge valid"><CheckCircle size={12} /> Trajectory built</span>
              ) : (
                <span className="status-badge empty"><AlertTriangle size={12} /> Not built</span>
              )}
            </div>

            {versionsOf(proj).length > 0 && (
              // Clicks inside stay inside: the card itself is a "load" target.
              <details className="vault-versions" onClick={(e) => e.stopPropagation()}>
                <summary><History size={12} /> Save history ({versionsOf(proj).length})</summary>
                <ol>
                  {versionsOf(proj).map((v) => (
                    <li key={v.id}>
                      <span className="vault-version-when">{fmtSavedAt(v.savedAt)}</span>
                      <span className="vault-version-meta">
                        {v.label} · {v.scenes?.length ?? 0} scenes · {v.dronesList?.length ?? 0} vehicles
                      </span>
                      <button
                        type="button" className="btn-secondary btn-sm"
                        onClick={() => onRestore(proj.id, v.id)}
                        aria-label={`Restore ${proj.name} to ${fmtSavedAt(v.savedAt)}`}
                      >
                        Restore
                      </button>
                    </li>
                  ))}
                </ol>
              </details>
            )}

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
