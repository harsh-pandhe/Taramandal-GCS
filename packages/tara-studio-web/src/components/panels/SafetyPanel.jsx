import { CheckCircle, AlertTriangle } from 'lucide-react';
import { EmptyState } from '../common/index.jsx';
import { judgeMetrics } from '../../engine/safetyLimits.js';

const Metric = ({ title, value, unit, status, limitLabel }) => (
  <div className="metric-card" data-metric-status={status}>
    <div className="metric-title">{title}</div>
    <div className={`metric-val ${status === 'unknown' ? '' : status}`}>{value}{unit}</div>
    <div className="metric-limit">{limitLabel}</div>
  </div>
);

/** Results of the backend's 4D pre-flight safety validation. */
export default function SafetyPanel({ validation }) {
  if (!validation) {
    return (
      <EmptyState icon={CheckCircle} title="No validation yet">
        Generate a sequence to run pre-flight safety diagnostics.
      </EmptyState>
    );
  }

  const m = validation.metrics;
  const v = validation.violations || { proximity: [], velocity: [], acceleration: [] };
  const total = v.proximity.length + v.velocity.length + v.acceleration.length;
  const passed = validation.status === 'PASSED';

  return (
    <div className="glass-panel-inner">
      <h4 className="section-subtitle">Safety Audit</h4>

      <div className={`validation-summary ${validation.status.toLowerCase()}`}>
        {passed ? <CheckCircle size={18} /> : <AlertTriangle size={18} />}
        <span>{validation.summary}</span>
      </div>

      {m && (
        <div className="metric-grid">
          {/* Thresholds are the backend's live SafetyLimits (validation.limits), never local constants. */}
          {judgeMetrics(m, validation.limits).map(({ key, ...card }) => <Metric key={key} {...card} />)}
        </div>
      )}

      <span className="telemetry-label">Violations ({total})</span>
      <div className="violations-list">
        {v.proximity.map((x, i) => (
          <div key={`p-${i}`} className="violation-item">
            <span>Proximity breach (D{x.drones[0]} &amp; D{x.drones[1]})</span>
            <span className="v-danger">{x.distance}m @ {x.time}s</span>
          </div>
        ))}
        {v.velocity.map((x, i) => (
          <div key={`v-${i}`} className="violation-item">
            <span>Speed · Drone {x.drone}</span>
            <span className="v-warn">{x.velocity}m/s @ {x.time}s</span>
          </div>
        ))}
        {v.acceleration.map((x, i) => (
          <div key={`a-${i}`} className="violation-item">
            <span>Accel · Drone {x.drone}</span>
            <span className="v-warn">{x.acceleration}m/s² @ {x.time}s</span>
          </div>
        ))}
        {total === 0 && <div className="violations-none">Zero failsafe exceptions raised.</div>}
      </div>
    </div>
  );
}
