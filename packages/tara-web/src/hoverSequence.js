// The dashboard's side of the supervised single-drone hover (#171): what POST /api/single-drone/hover-sequence
// answers, as text an operator can read. The backend's own words are shown as they are; nothing here decides
// whether a hover is safe (the GCS gates, the pre-arm checks and the fleet state do).

/** The roles that may start a hover (the GCS refuses anything below FLIGHT_COMMANDER). */
export const HOVER_ROLES = ['FLIGHT_COMMANDER'];

/**
 * An error `detail` from the GCS as one string. FastAPI sends a string for most refusals, but the pre-arm
 * refusal (412) sends { message, profile, blocking_reasons: [...] }, which must not be shown as "[object Object]".
 */
export function hoverErrorText(detail, fallback = 'The hover sequence was refused.') {
  if (detail === null || detail === undefined || detail === '') return fallback;
  if (typeof detail === 'string') return detail;
  if (typeof detail === 'object') {
    const message = typeof detail.message === 'string' ? detail.message : fallback;
    const reasons = Array.isArray(detail.blocking_reasons) ? detail.blocking_reasons.filter((r) => typeof r === 'string') : [];
    return reasons.length ? `${message} ${reasons.join('; ')}` : message;
  }
  return String(detail);
}

const STEP_LABELS = {
  PREFLIGHT: 'Pre-arm gates',
  ARM: 'Arm',
  HOVER_REACHED: 'Hover reached',
  HOVER_HOLD_COMPLETE: 'Hover held',
  LANDING_COMMANDED: 'Landing',
  TOUCHDOWN_DISARM: 'Touchdown and disarm',
};

/** The steps of a finished sequence as [{ key, label, status, detail }], in the order the GCS reported them. */
export function hoverSteps(body) {
  const steps = Array.isArray(body?.steps) ? body.steps : [];
  return steps.map((s, i) => {
    const bits = [];
    if (Number.isFinite(s?.altitude_m)) bits.push(`${s.altitude_m.toFixed(2)} m`);
    if (Number.isFinite(s?.dwell_s)) bits.push(`${s.dwell_s.toFixed(1)} s`);
    return { key: `${i}-${s?.step}`, label: STEP_LABELS[s?.step] ?? String(s?.step ?? 'Step'), status: String(s?.status ?? ''), detail: bits.join(', ') };
  });
}

/** Whether the control may be used, and if not, why (shown next to the disabled button). */
export function hoverAvailability({ mode, role, connectedCount, running }) {
  if (mode !== 'SIMULATION') return { ok: false, reason: 'Hover test is simulation only until the hardware gate (#135) is passed.' };
  if (!HOVER_ROLES.includes(role)) return { ok: false, reason: 'Needs a FLIGHT_COMMANDER session.' };
  if (!connectedCount) return { ok: false, reason: 'No drone is connected.' };
  if (running) return { ok: false, reason: 'A hover sequence is running.' };
  return { ok: true, reason: '' };
}
