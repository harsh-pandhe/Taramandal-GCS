import { CheckCircle, AlertTriangle, Loader2 } from 'lucide-react';
import { EmptyState } from '../common/index.jsx';
import { judgeMetrics } from '../../engine/safetyLimits.js';
import EnduranceSection from './EnduranceSection.jsx';

const Metric = ({ title, value, unit, status, limitLabel }) => (
  <div className="metric-card" data-metric-status={status}>
    <div className="metric-title">{title}</div>
    <div className={`metric-val ${status === 'unknown' ? '' : status}`}>{value}{unit}</div>
    <div className="metric-limit">{limitLabel}</div>
  </div>
);

/** Results of the backend's 4D pre-flight safety validation, plus battery endurance. */
export default function SafetyPanel({ validation, validating, choreoData }) {
  if (!validation) {
    if (choreoData) {
      return (
        <div className="glass-panel-inner">
          {/* Issue #173: validation runs in the background after a build and can take
              several seconds at large fleet sizes (~15s at 1000 drones) - without this,
              the panel showed nothing safety-related for that whole window, no different
              from "never validated", which is exactly the "stale/no feedback" gap the
              issue's acceptance criteria calls out. */}
          {validating && (
            <div className="validation-summary unverified" role="status" aria-live="polite">
              <Loader2 size={18} className="anim-spin" />
              <span>Validating — running pre-flight safety and dynamics checks…</span>
            </div>
          )}
          <EnduranceSection choreoData={choreoData} />
        </div>
      );
    }
    return (
      <EmptyState icon={CheckCircle} title="No validation yet">
        Generate a sequence to run pre-flight safety diagnostics.
      </EmptyState>
    );
  }

  const m = validation.metrics;
  const v = validation.violations || {};
  // /api/validate (validate_show) never includes a "jerk" key at all - only
  // /api/generate adds one, from a separate validate_dynamics call (issue
  // #151). Defaulting every category the same way means a plain re-validate
  // never silently drops a category that happened to come from the other
  // endpoint last time, rather than genuinely having zero violations. Same
  // reasoning already applied to altitude/ground (issue #148) below.
  const proximityViolations = v.proximity || [];
  const velocityViolations = v.velocity || [];
  const accelerationViolations = v.acceleration || [];
  const yawRateViolations = v.yaw_rate || [];
  const jerkViolations = v.jerk || [];
  const altitudeViolations = v.altitude || [];
  const groundViolations = v.ground || [];
  const total = proximityViolations.length + velocityViolations.length + accelerationViolations.length
    + yawRateViolations.length + jerkViolations.length + altitudeViolations.length + groundViolations.length;
  const passed = validation.status === 'PASSED';
  // Issue #151: a fresh /api/validate or /api/generate call always carries a
  // jerk metric and its limit now (certify_feasibility_report), so "unknown"
  // is reserved for a response from before that - either the metric itself
  // is missing (legacy /api/validate), or validation.limits wasn't attached
  // at all (a response from before issue #97).
  const hasJerkMetric = m && m.max_jerk_m_s3 !== undefined && validation.limits?.max_jerk_ms3 !== undefined;

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
          {/* Always rendered, like every other metric card (issue #151): a
              plain /api/validate response never ran validate_dynamics, so
              hiding this card instead of marking it unknown could read as
              "no jerk problem" when it's really "not checked this time". */}
          <Metric
            key="jerk"
            title="Max Jerk"
            value={hasJerkMetric ? m.max_jerk_m_s3 : '—'}
            unit={hasJerkMetric ? 'm/s³' : ''}
            status={
              !hasJerkMetric ? 'unknown'
                : m.max_jerk_m_s3 <= validation.limits.max_jerk_ms3 ? 'passed' : 'danger'
            }
            limitLabel={hasJerkMetric ? `≤ ${validation.limits.max_jerk_ms3} m/s³` : 'not checked by this request'}
          />
        </div>
      )}

      <span className="telemetry-label">Violations ({total})</span>
      <div className="violations-list">
        {proximityViolations.map((x, i) => (
          <div key={`p-${i}`} className="violation-item">
            <span>Proximity breach (D{x.drones[0]} &amp; D{x.drones[1]})</span>
            <span className="v-danger">{x.distance}m @ {x.time}s</span>
          </div>
        ))}
        {velocityViolations.map((x, i) => (
          <div key={`v-${i}`} className="violation-item">
            <span>Speed · Drone {x.drone}</span>
            <span className="v-warn">{x.velocity}m/s @ {x.time}s</span>
          </div>
        ))}
        {accelerationViolations.map((x, i) => (
          <div key={`a-${i}`} className="violation-item">
            <span>Accel · Drone {x.drone}</span>
            <span className="v-warn">{x.acceleration}m/s² @ {x.time}s</span>
          </div>
        ))}
        {yawRateViolations.map((x, i) => (
          <div key={`y-${i}`} className="violation-item">
            <span>Yaw rate · Drone {x.drone}</span>
            <span className="v-warn">{x.yaw_rate}°/s @ {x.time}s</span>
          </div>
        ))}
        {jerkViolations.map((x, i) => (
          <div key={`j-${i}`} className="violation-item">
            <span>Jerk · Drone {x.drone}</span>
            <span className="v-warn">{x.jerk}m/s³ @ {x.time}s</span>
          </div>
        ))}
        {altitudeViolations.map((x, i) => (
          <div key={`alt-${i}`} className="violation-item">
            <span>Altitude · Drone {x.drone}</span>
            <span className="v-danger">{x.altitude}m @ {x.time}s</span>
          </div>
        ))}
        {groundViolations.map((x, i) => (
          <div key={`gnd-${i}`} className="violation-item">
            <span>Below ground · Drone {x.drone}</span>
            <span className="v-danger">{x.altitude}m @ {x.time}s</span>
          </div>
        ))}
        {total === 0 && <div className="violations-none">Zero failsafe exceptions raised.</div>}
      </div>

      {choreoData && <EnduranceSection choreoData={choreoData} />}
    </div>
  );
}
