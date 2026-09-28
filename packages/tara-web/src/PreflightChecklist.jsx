import React, { useState } from 'react';
import { STATE_LABEL, summarizeRules } from './preflight';

// Pre-arm checklist HUD and launch modal (issue #48).
//
// Every gate is evaluated by the GCS (GET /api/preflight-check) under the
// safety profile tara.toml configures, and /api/launch re-evaluates and refuses
// with a 412 if any blocking gate is red. The dashboard only renders that
// verdict: it never decides on its own that a launch is safe, so a stale or
// tampered browser cannot arm a fleet the GCS would refuse.

export function InterlockCards({ report, error }) {
  if (!report) {
    return (
      <div className="interlock-empty" data-testid="interlock-unavailable">
        Pre-arm status unavailable{error ? ` (${error})` : ''} — launch is locked.
      </div>
    );
  }
  return (
    <div className="interlock-grid">
      {summarizeRules(report).map((rule) => (
        <div
          key={rule.id}
          className={`interlock-card interlock-${rule.state}`}
          data-testid={`interlock-${rule.id}`}
          data-state={rule.state}
        >
          <div className="interlock-title">
            <span>{rule.title}</span>
            <span className="interlock-state">{STATE_LABEL[rule.state]}</span>
          </div>
          <div className="interlock-detail">{rule.detail}</div>
          {rule.failures.slice(0, 3).map((f) => (
            <div key={f.id} className="interlock-failure">
              D{String(f.id).padStart(2, '0')}: {f.detail}
            </div>
          ))}
          {rule.failures.length > 3 && (
            <div className="interlock-failure">+{rule.failures.length - 3} more</div>
          )}
        </div>
      ))}
    </div>
  );
}

export function ProfileBadge({ report }) {
  return (
    <span className="profile-badge" data-testid="safety-profile">
      {report ? report.profile : 'UNKNOWN'}
    </span>
  );
}

async function postJson(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    // An empty or non-JSON body still has a status worth reporting.
  }
  return { ok: res.ok, status: res.status, data };
}

function detailText(data, fallback) {
  const d = data?.detail;
  if (!d) return data?.message || fallback;
  if (typeof d === 'string') return d;
  return [d.message, ...(d.blocking_reasons || [])].filter(Boolean).join(' · ');
}

// The two-step flows (launch latch, override) share a shape: step 1 returns a
// token, step 2 sends it back. Each click is its own HTTP request, so a
// confirmation cannot happen by accident in one gesture.
function TwoStepControl({ label, confirmLabel, pending, onRequest, onConfirm, busy, disabled, testId }) {
  return pending ? (
    <button className="btn btn-land" onClick={onConfirm} disabled={busy} data-testid={`${testId}-confirm`}>
      {confirmLabel}
    </button>
  ) : (
    <button className="btn btn-stop" onClick={onRequest} disabled={busy || disabled} data-testid={`${testId}-request`}>
      {label}
    </button>
  );
}

export function PreflightModal({ apiBase, report, error, operator = 'operator', onClose, onLaunched }) {
  const [latchToken, setLatchToken] = useState(null);
  const [overrideToken, setOverrideToken] = useState(null);
  const [overrideReason, setOverrideReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  const permitted = Boolean(report?.launch_permitted);
  const latchRule = report?.rules.find((r) => r.id === 'operator_latch');
  const latchRequired = latchRule && latchRule.severity !== 'IGNORED';
  const latchState = report?.confirmations?.latch?.state ?? 'idle';
  const overridable = summarizeRules(report).filter((r) => r.overridable && r.state === 'fail');

  const run = async (fn) => {
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      setMessage(`API error: ${err.message}`);
    } finally {
      setBusy(false);
    }
  };

  const requestLatch = () => run(async () => {
    const r = await postJson(`${apiBase}/api/preflight/latch/request`, { operator });
    if (r.ok) {
      setLatchToken(r.data.token);
      setMessage(`Latch armed — confirm within ${r.data.expires_in_s}s.`);
    } else {
      setMessage(detailText(r.data, 'Latch request failed'));
    }
  });

  const confirmLatch = () => run(async () => {
    const r = await postJson(`${apiBase}/api/preflight/latch/confirm`, { token: latchToken, operator });
    setLatchToken(null);
    setMessage(r.ok ? 'Launch latch confirmed.' : detailText(r.data, 'Latch confirmation failed'));
  });

  const requestOverride = () => run(async () => {
    const r = await postJson(`${apiBase}/api/preflight/override/request`, {
      rules: overridable.map((x) => x.id),
      reason: overrideReason,
      operator,
    });
    if (r.ok) {
      setOverrideToken(r.data.token);
      setMessage(`Override of ${r.data.rules.join(', ')} requested — confirm within ${r.data.expires_in_s}s.`);
    } else {
      setMessage(detailText(r.data, 'Override request failed'));
    }
  });

  const confirmOverride = () => run(async () => {
    const r = await postJson(`${apiBase}/api/preflight/override/confirm`, { token: overrideToken, operator });
    setOverrideToken(null);
    setMessage(r.ok ? 'Override confirmed and logged.' : detailText(r.data, 'Override confirmation failed'));
  });

  const launch = () => run(async () => {
    const r = await postJson(`${apiBase}/api/launch`);
    if (r.ok) {
      onLaunched?.(r.data);
      onClose?.();
    } else {
      setMessage(detailText(r.data, `Launch refused (HTTP ${r.status})`));
    }
  });

  return (
    <div className="preflight-backdrop" role="dialog" aria-modal="true" aria-labelledby="preflight-title">
      <div className="preflight-modal" data-testid="preflight-modal">
        <div className="preflight-header">
          <h2 id="preflight-title">PRE-ARM CHECKLIST</h2>
          <ProfileBadge report={report} />
          <button className="preflight-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <InterlockCards report={report} error={error} />

        {report && report.blocking_reasons.length > 0 && (
          <ul className="preflight-reasons" data-testid="blocking-reasons">
            {report.blocking_reasons.slice(0, 8).map((r) => <li key={r}>{r}</li>)}
            {report.blocking_reasons.length > 8 && <li>+{report.blocking_reasons.length - 8} more</li>}
          </ul>
        )}

        {report && permitted && report.excluded_drone_ids.length > 0 && (
          <div className="preflight-note">
            Partial-fleet profile: drones {report.excluded_drone_ids.join(', ')} will stay on the ground.
          </div>
        )}

        {overridable.length > 0 && (
          <div className="preflight-override">
            <label htmlFor="override-reason">
              Override {overridable.map((r) => r.title).join(', ')} — reason (logged to the audit trail)
            </label>
            <input
              id="override-reason"
              value={overrideReason}
              onChange={(e) => setOverrideReason(e.target.value)}
              placeholder="e.g. bench test, no GNSS antenna fitted"
              data-testid="override-reason"
            />
            <TwoStepControl
              label="1. REQUEST OVERRIDE"
              confirmLabel="2. CONFIRM OVERRIDE"
              pending={Boolean(overrideToken)}
              onRequest={requestOverride}
              onConfirm={confirmOverride}
              busy={busy}
              disabled={!overrideReason.trim()}
              testId="override"
            />
          </div>
        )}

        {message && <div className="preflight-message" data-testid="preflight-message">{message}</div>}

        <div className="preflight-actions">
          {latchRequired && latchState !== 'confirmed' && (
            <TwoStepControl
              label="1. ARM LAUNCH LATCH"
              confirmLabel="2. CONFIRM LATCH"
              pending={Boolean(latchToken)}
              onRequest={requestLatch}
              onConfirm={confirmLatch}
              busy={busy}
              testId="latch"
            />
          )}
          <button
            className="btn btn-launch"
            onClick={launch}
            disabled={!permitted || busy}
            data-testid="preflight-launch"
            title={permitted ? 'All blocking gates are green' : 'Blocked: one or more pre-arm gates are red'}
          >
            {permitted ? 'LAUNCH FLEET' : 'LAUNCH BLOCKED'}
          </button>
        </div>
      </div>
    </div>
  );
}
