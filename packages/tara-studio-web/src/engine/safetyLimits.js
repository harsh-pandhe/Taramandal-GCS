// Pass/fail judgement for the Safety tab's metric cards (issue #97).
//
// Every threshold comes from the `limits` object the backend attaches to its
// validation report: tara_core.safety.DEFAULT_LIMITS, the same SafetyLimits the
// validator and the GCS use. There are deliberately no numeric fallbacks here.
// A hardcoded copy is exactly what drifted before (0.8m separation shown green
// against a 1.5m runtime abort). With no limits (a project saved before the
// backend sent them, or an older backend), a metric is shown unjudged instead.

const fmt = (v) => (Number.isInteger(v) ? String(v) : v.toFixed(1));

/**
 * @param {object} metrics  validation.metrics from /api/validate
 * @param {object} [limits] validation.limits (SafetyLimits fields)
 * @returns {Array<{key, title, value, unit, status, limitLabel}>} one entry per card,
 *   status being 'passed' | 'warning' | 'danger' | 'unknown'.
 */
export function judgeMetrics(metrics, limits) {
  const has = (k) => typeof limits?.[k] === 'number';

  // The validator certifies separation against design_separation_m. Between
  // that and min_separation_m the show fails validation but would not yet trip
  // the fleet's runtime abort, so it gets its own band.
  let sepStatus = 'unknown';
  if (has('design_separation_m') && has('min_separation_m')) {
    if (metrics.min_distance_m >= limits.design_separation_m) sepStatus = 'passed';
    else if (metrics.min_distance_m >= limits.min_separation_m) sepStatus = 'warning';
    else sepStatus = 'danger';
  }

  const ceiling = (value, key) => {
    if (!has(key)) return 'unknown';
    return value <= limits[key] ? 'passed' : 'danger';
  };

  const cards = [
    {
      key: 'separation',
      title: 'Min Sep',
      value: metrics.min_distance_m,
      unit: 'm',
      status: sepStatus,
      limitLabel: has('design_separation_m') && has('min_separation_m')
        ? `≥ ${fmt(limits.design_separation_m)}m · abort < ${fmt(limits.min_separation_m)}m`
        : 'limit unknown',
    },
    {
      key: 'velocity',
      title: 'Max Speed',
      value: metrics.max_velocity_m_s,
      unit: 'm/s',
      status: ceiling(metrics.max_velocity_m_s, 'max_velocity_ms'),
      limitLabel: has('max_velocity_ms') ? `≤ ${fmt(limits.max_velocity_ms)} m/s` : 'limit unknown',
    },
    {
      key: 'acceleration',
      title: 'Max Accel',
      value: metrics.max_acceleration_m_s2,
      unit: 'm/s²',
      status: ceiling(metrics.max_acceleration_m_s2, 'max_accel_ms2'),
      limitLabel: has('max_accel_ms2') ? `≤ ${fmt(limits.max_accel_ms2)} m/s²` : 'limit unknown',
    },
    {
      key: 'yaw',
      title: 'Yaw Rate',
      value: Math.round(metrics.max_yaw_rate_deg_s),
      unit: '°/s',
      status: ceiling(metrics.max_yaw_rate_deg_s, 'max_yaw_rate_dps'),
      limitLabel: has('max_yaw_rate_dps') ? `≤ ${fmt(limits.max_yaw_rate_dps)} °/s` : 'limit unknown',
    },
  ];

  // Issue #148: before the backend enforced the ceiling at all, there was no
  // altitude metric to show. A report from before that change (an older
  // backend, or a cached result) simply has no `max_altitude_m` key — the
  // card is left out entirely rather than shown with an undefined value.
  if (typeof metrics.max_altitude_m === 'number') {
    cards.push({
      key: 'altitude',
      title: 'Max Altitude',
      value: metrics.max_altitude_m,
      unit: 'm',
      status: ceiling(metrics.max_altitude_m, 'max_altitude_m'),
      limitLabel: has('max_altitude_m') ? `≤ ${fmt(limits.max_altitude_m)} m` : 'limit unknown',
    });
  }

  return cards;
}
