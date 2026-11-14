import { AlertTriangle } from 'lucide-react';
import { violationsByScene, WHERE_TEXT } from '../../engine/violationScenes.js';
import { exportGate } from '../../engine/exportGate.js';

const SHOWN = 3;
const LABEL = { yaw_rate: 'yaw rate' };

/** "2 proximity, 1 velocity" - in the order the categories were counted. */
const countsText = (counts) => Object.entries(counts)
  .map(([category, n]) => `${n} ${LABEL[category] || category}`)
  .join(', ');

// A plain function, not a component, so the rows are part of the returned tree.
const renderItems = (items) => (
  <div className="na-items">
    {items.slice(0, SHOWN).map((it, i) => (
      <div key={`${it.category}-${it.time}-${i}`} className="violation-item na-item">
        <span>{it.text}</span>
        <span className="na-where">{WHERE_TEXT[it.where] || WHERE_TEXT.unknown}</span>
      </div>
    ))}
    {items.length > SHOWN && <div className="panel-hint">+{items.length - SHOWN} more</div>}
  </div>
);

/**
 * Plain-words list of what failed the safety check, grouped by the scene that
 * was playing when it happened (issue #92).
 *
 * Renders nothing unless the last check failed (as exportGate defines it) and there is something to show,
 * so every other state of the Sequence tab is exactly as it was. Every prop is
 * optional.
 */
export default function NeedsAttention({
  validation, segments, scenes, stale, onScrub, onSelectScene,
}) {
  // The app's own definition of failed (exportGate): a report that passes the hard limits
  // but is not flight-certified (jerk) is failed too, and is the case that most needs this list.
  if (!validation || !exportGate({ hasShow: true, validation }).isFailed) return null;
  const { groups, unassigned, stale: isStale } = violationsByScene({
    violations: validation.violations, segments, scenes, stale,
  });

  if (isStale) {
    return (
      <section className="needs-attention" aria-label="Needs attention">
        <h4 className="section-subtitle">Needs attention</h4>
        <p className="panel-hint">
          The scenes changed since the last safety check. Build again to refresh this list.
        </p>
      </section>
    );
  }
  if (!groups.length && !unassigned.length) return null;

  const jump = (t) => { if (onScrub) onScrub(t); };

  return (
    <section className="needs-attention" aria-label="Needs attention">
      <h4 className="section-subtitle"><AlertTriangle size={14} /> Needs attention</h4>

      {groups.map((g) => (
        <div key={g.sceneId} className="na-block">
          <div className="na-head">
            <strong>{String(g.index + 1).padStart(2, '0')} · {g.name || 'Untitled scene'}</strong>
            <span className="na-counts">{countsText(g.counts)}</span>
          </div>
          {renderItems(g.items)}
          <button
            type="button"
            className="btn-secondary btn-sm"
            onClick={() => { if (onSelectScene) onSelectScene(g.sceneId); jump(g.firstTime); }}
          >
            Go to scene
          </button>
        </div>
      ))}

      {unassigned.length > 0 && (
        <div className="na-block">
          <div className="na-head">
            <strong>Not tied to a scene</strong>
            <span className="na-counts">{unassigned.length}</span>
          </div>
          {renderItems(unassigned)}
          <button type="button" className="btn-secondary btn-sm" onClick={() => jump(unassigned[0].time)}>
            Jump to time
          </button>
        </div>
      )}

      <p className="panel-hint">
        The validator lists up to its reporting cap of the worst violations; counts here are not full totals.
      </p>
    </section>
  );
}
