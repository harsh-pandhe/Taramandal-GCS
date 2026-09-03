import { CheckCircle, AlertTriangle } from 'lucide-react';
import { EmptyState } from '../common/index.jsx';

const Metric = ({ title, value, unit, ok }) => (
  <div className="metric-card">
    <div className="metric-title">{title}</div>
    <div className={`metric-val ${ok ? 'passed' : 'warning'}`}>{value}{unit}</div>
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
          <Metric title="Min Sep" value={m.min_distance_m} unit="m" ok={m.min_distance_m >= 0.8} />
          <Metric title="Max Speed" value={m.max_velocity_m_s} unit="m/s" ok={m.max_velocity_m_s <= 4.0} />
          <Metric title="Max Accel" value={m.max_acceleration_m_s2} unit="m/s²" ok={m.max_acceleration_m_s2 <= 2.0} />
          <Metric title="Yaw Rate" value={Math.round(m.max_yaw_rate_deg_s)} unit="°/s" ok={m.max_yaw_rate_deg_s <= 180} />
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
