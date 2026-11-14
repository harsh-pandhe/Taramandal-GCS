import { Wand2 } from 'lucide-react';
import { violationLines } from '../../engine/proposalView.js';

const STATUS_CLASS = { PASSED: 'passed', FAILED: 'failed', REFUSED: 'failed', UNVERIFIED: 'unverified', CHECKING: 'unverified' };

/**
 * One typed scene edit, shown before it touches anything (issue #89): what it
 * will change, the result of a dry-run safety check, and Apply only once that
 * check PASSED. State and rules live in useIntentProposal and
 * engine/proposalView.js; this only draws them. Apply runs the one history
 * command, so a single undo reverts the whole edit.
 */
export default function ProposalPanel({ intent }) {
  const { text, setText, proposal, check, error, block, preview, apply, dismiss } = intent;
  const checking = check?.status === 'CHECKING';
  const settled = check && !checking;
  const violations = settled ? violationLines(check.violations) : [];

  return (
    <div className="glass-panel-inner" data-testid="edit-panel">
      <h4 className="section-subtitle">Scene Edit</h4>
      <p className="ai-panel-hint">
        Type one edit in plain words, such as "set hold of scene 1 to 8 s". It is previewed and safety
        checked first; nothing changes until you apply it.
      </p>

      <label className="ui-field">
        <span className="ui-field-label">Edit request</span>
        <input
          type="text"
          maxLength={200}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); preview(); } }}
          placeholder="make scene 2 a heart"
        />
      </label>

      <button type="button" className="btn-secondary btn-inline" onClick={preview} disabled={!text.trim() || checking}>
        <Wand2 size={14} /> Preview edit
      </button>

      {error && <div className="validation-summary failed" role="alert">{error}</div>}

      {proposal && (
        <div className="ai-proposal-card" data-testid="edit-proposal">
          <h5 className="ai-proposal-name">{proposal.summary}</h5>
          {proposal.changes.length > 0 && (
            <dl className="ai-proposal-fields">
              {proposal.changes.map((c, i) => (
                <div key={i} style={{ display: 'contents' }}><dt>{c.label}</dt><dd>{c.value}</dd></div>
              ))}
            </dl>
          )}

          {checking && <div className="validation-summary unverified">Checking safety…</div>}
          {settled && (
            <div className={`validation-summary ${STATUS_CLASS[check.status] ?? 'unverified'}`} data-testid="edit-check">
              <span>{check.status}: {check.summary}</span>
            </div>
          )}
          {violations.length > 0 && (
            <div className="violations-list">
              {violations.map((v, i) => <div key={i} className="violation-item">{v}</div>)}
            </div>
          )}
          {block && (block.kind === 'stale' || block.kind === 'context') && (
            <div className="validation-summary unverified" role="alert">{block.reason}</div>
          )}

          <div className="ai-proposal-actions">
            <button type="button" className="btn-primary btn-inline" onClick={apply} disabled={!!block}>
              Apply edit
            </button>
            <button type="button" className="btn-ghost btn-inline" onClick={dismiss}>
              Dismiss edit
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
