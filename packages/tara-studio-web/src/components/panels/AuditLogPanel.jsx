import { useEffect, useRef, useState } from 'react';
import { createAuditClient } from '../../engine/auditClient.js';
import { createLatestRun } from '../../engine/latestRun.js';
import { LIST_LIMIT, MESSAGES, loadAuditView } from '../../engine/auditView.js';

const LOADING = { kind: 'loading', projectId: null };

/**
 * The AI decision log (issue #94): the latest decisions recorded for the open project,
 * newest first, and a quiet note when the last one could not be recorded. Read-only: it
 * has no control that edits, deletes or sends anything, and nothing in the editing flow
 * waits on it. Reading and wording live in engine/auditView.js; this only draws them.
 *
 * It reads again when it appears (the AI tab opens), when the open project changes and
 * when `auditVersion` moves (a decision was just recorded). Only the newest read may fill
 * it in: a slower, older answer is dropped, and so is one that arrives after it is gone.
 */
export default function AuditLogPanel({ intent, client = null }) {
  const { auditNote = null, auditVersion = 0, auditProjectId = null } = intent;
  const [view, setView] = useState(LOADING);
  const runRef = useRef(null);
  if (runRef.current === null) runRef.current = createLatestRun();
  const clientRef = useRef(null);

  useEffect(() => {
    const run = runRef.current;
    const id = run.begin();
    let gone = false;
    // Rows of the same project stay on screen while they are read again; another project's do not.
    setView((v) => (v.projectId === auditProjectId && v.kind !== 'loading' ? v : { ...LOADING, projectId: auditProjectId }));
    if (!auditProjectId) {
      setView({ kind: 'noproject', projectId: null });
      return undefined;
    }
    clientRef.current ??= client ?? createAuditClient();
    loadAuditView(clientRef.current, auditProjectId, LIST_LIMIT).then((next) => {
      if (!gone && run.isCurrent(id)) setView({ ...next, projectId: auditProjectId });
    });
    return () => { gone = true; run.invalidate(); };
  }, [auditProjectId, auditVersion, client]);

  const notices = view.notices ?? [];
  return (
    <div className="glass-panel-inner audit-log" data-testid="audit-log">
      <h4 className="section-subtitle">AI decision log</h4>

      {auditNote && (
        <div className={`audit-note${auditNote.kind === 'skipped' ? '' : ' warn'}`} role="status" data-testid="audit-note">
          {auditNote.message}
        </div>
      )}

      {view.kind === 'noproject' && <p className="ai-panel-hint" data-testid="audit-state">{MESSAGES.noProject}</p>}
      {view.kind === 'loading' && <p className="ai-panel-hint" data-testid="audit-state">{MESSAGES.loading}</p>}
      {view.kind === 'empty' && <p className="ai-panel-hint" data-testid="audit-state">{MESSAGES.empty}</p>}
      {view.kind === 'error' && <p className="ai-panel-hint" data-testid="audit-state">{MESSAGES.error}</p>}
      {notices.map((message) => (
        <div key={message} className="audit-note warn" role="status" data-testid="audit-log-notice">{message}</div>
      ))}

      {view.kind === 'ready' && (
        <ol className="audit-list" aria-label="AI decision log" data-testid="audit-list">
          {view.rows.map((row) => (
            <li key={row.key} className="audit-row" data-testid="audit-row">
              <div className="audit-head">
                <span className="audit-decision">{row.decision}</span>
                {row.source && <span>{row.source}</span>}
                <span>{row.when}</span>
              </div>
              {row.request && <p className="audit-text">{row.request}</p>}
              {row.summary && <p className="audit-text audit-dim">{row.summary}</p>}
              {row.reason && <p className="audit-text audit-dim">{row.reason}</p>}
              {row.check && <p className="audit-text audit-dim">{row.check}</p>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
