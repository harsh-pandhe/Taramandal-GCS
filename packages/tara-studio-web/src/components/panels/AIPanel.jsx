import { useRef, useState } from 'react';
import { Sparkles, Upload, X } from 'lucide-react';
import { sanitizeSvg } from '../../engine/svgImporter.js';
import { FORMATION_LABELS } from '../../engine/formations.js';

const SVG_ACCEPT = 'image/svg+xml,.svg';

/** A short, human label for a proposal's shape, e.g. "Heart" or "Your uploaded SVG". */
const shapeLabel = (p) => {
  if (p.shape_type === 'svg') return 'Your uploaded SVG';
  if (p.shape_type === 'text') return `Digit "${p.shape_payload}"`;
  return FORMATION_LABELS[p.shape_payload] || p.shape_payload;
};

const STATUS_MESSAGES = {
  unavailable: 'Ollama is not reachable. Make sure it is running locally, then try again.',
  timeout: 'The local model took too long to respond. Try again, or a shorter request.',
  schema_error: "Couldn't turn that into a usable scene, even after one retry. Try rephrasing.",
};

/**
 * Narrow local AI authoring (issue #133): one typed scene proposal at a
 * time, reviewed and explicitly applied or discarded. Applying a proposal
 * calls the exact same `addScene` a manual "Add Scene" click would (passed
 * down from `useAIProposal`, which owns all the state here so it survives a
 * tab switch), so it goes through Studio's existing undo history, geometry
 * build and safety validator unchanged — this panel never builds or
 * certifies anything itself, it only ever produces, or discards, one
 * scene-add patch.
 */
export default function AIPanel({ ai }) {
  const { prompt, setPrompt, svg, setSvg, proposing, proposal, requestError, stale, propose, apply, discard } = ai;
  const [svgError, setSvgError] = useState(null);
  const fileRef = useRef(null);

  const onFile = (e) => {
    const f = e.target.files?.[0];
    e.target.value = '';
    if (!f) return;
    f.text().then((text) => {
      try {
        setSvg({ name: f.name, text: sanitizeSvg(text) });
        setSvgError(null);
      } catch (err) {
        setSvg(null);
        setSvgError(err.message);
      }
    });
  };

  return (
    <div className="glass-panel-inner">
      <h4 className="section-subtitle">AI Scene Proposal</h4>
      <p className="ai-panel-hint">
        Describe one scene. The model only ever picks from Studio's existing shapes and timing
        fields — it never touches safety limits, geometry, or flight control.
      </p>

      <label className="ui-field">
        <span className="ui-field-label">Prompt</span>
        <textarea
          className="ai-prompt-input"
          rows={3}
          maxLength={2000}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder='e.g. "A heart shape that holds for eight seconds"'
          disabled={proposing}
        />
      </label>

      <input ref={fileRef} type="file" accept={SVG_ACCEPT} className="visually-hidden" onChange={onFile} />
      {!svg ? (
        <button type="button" className="btn-secondary btn-inline" onClick={() => fileRef.current?.click()} disabled={proposing}>
          <Upload size={14} /> Attach an SVG (optional)
        </button>
      ) : (
        <div className="audio-loaded-row">
          <div className="audio-meta">
            <span className="mono audio-name" title={svg.name}>{svg.name}</span>
          </div>
          <button type="button" className="btn-ghost btn-tiny" onClick={() => setSvg(null)} title="Remove SVG" disabled={proposing}>
            <X size={13} />
          </button>
        </div>
      )}
      {svgError && <div className="access-error-msg">{svgError}</div>}

      <button
        type="button"
        className="btn-primary btn-inline ai-propose-btn"
        onClick={propose}
        disabled={proposing || !prompt.trim()}
      >
        <Sparkles size={14} /> {proposing ? 'Thinking…' : 'Propose'}
      </button>

      {requestError && <div className="validation-summary failed">{requestError}</div>}

      {proposal && proposal.status !== 'ok' && (
        <div className={`validation-summary ${proposal.status === 'schema_error' ? 'failed' : 'unverified'}`}>
          {STATUS_MESSAGES[proposal.status] || proposal.error || 'The proposal could not be generated.'}
        </div>
      )}

      {proposal && proposal.status === 'ok' && (
        <div className="ai-proposal-card">
          {stale && (
            <div className="validation-summary unverified">
              Scenes changed since this proposal was made — re-propose before applying.
            </div>
          )}
          <h5 className="ai-proposal-name">{proposal.name}</h5>
          <dl className="ai-proposal-fields">
            <dt>Shape</dt><dd>{shapeLabel(proposal)}</dd>
            <dt>Scale</dt><dd>{proposal.scale.toFixed(2)}×</dd>
            <dt>Hold</dt><dd>{proposal.hold_seconds.toFixed(1)}s</dd>
            <dt>Transition</dt><dd>{proposal.transition_seconds.toFixed(1)}s</dd>
          </dl>
          {proposal.unsupported?.length > 0 && (
            <>
              <div className="ai-unsupported-label">Not represented in this scene:</div>
              <ul className="ai-unsupported-list">
                {proposal.unsupported.map((u, i) => <li key={i}>{u}</li>)}
              </ul>
            </>
          )}
          <div className="ai-proposal-actions">
            <button type="button" className="btn-primary btn-inline" onClick={apply} disabled={stale}>
              Apply
            </button>
            <button type="button" className="btn-ghost btn-inline" onClick={discard}>
              Discard
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
