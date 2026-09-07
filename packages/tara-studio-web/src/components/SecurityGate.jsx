import { useState } from 'react';

/**
 * Local demo access gate.
 *
 * This is a UI-level barrier for local/demo builds only — the passphrase lives
 * in the bundle, so it keeps a casual visitor out of a running demo and nothing
 * more. It is not authentication and must not be treated as protecting anything
 * the backend doesn't already protect. Set VITE_DISABLE_AUTH=true to skip it.
 */
export default function SecurityGate({ onUnlock }) {
  const [passcode, setPasscode] = useState('');
  const [error, setError] = useState('');

  const submit = (e) => {
    e.preventDefault();
    if (passcode.toUpperCase() === 'TARAMANDAL_2026') {
      sessionStorage.setItem('taramandal_unlocked', 'true');
      setError('');
      onUnlock();
    } else {
      setError('ACCESS DENIED: INVALID SYSTEM PASSKEY');
    }
  };

  return (
    <div className="security-gate-wrapper">
      <div className="security-gate-card">
        <svg className="gate-logo" viewBox="0 0 64 64" fill="none" aria-hidden="true">
          <ellipse cx="32" cy="32" rx="26" ry="10" stroke="var(--primary)" strokeWidth="1.5" transform="rotate(-30 32 32)" strokeDasharray="3 3" />
          <ellipse cx="32" cy="32" rx="26" ry="10" stroke="var(--primary)" strokeWidth="1.5" transform="rotate(30 32 32)" strokeDasharray="3 3" />
          <circle cx="32" cy="32" r="7" fill="#0d1426" stroke="#818cf8" strokeWidth="2" />
          <circle cx="15" cy="22" r="3" fill="var(--primary)" />
          <circle cx="49" cy="42" r="3" fill="var(--primary)" />
          <circle cx="49" cy="22" r="3" fill="#818cf8" />
          <circle cx="15" cy="42" r="3" fill="#818cf8" />
        </svg>

        <div className="gate-brand">
          <h1>TARAMANDAL AEROSPACE</h1>
          <p>FLIGHT TRAJECTORY CONTROL ENGINE</p>
        </div>

        <form className="gate-form" onSubmit={submit}>
          <label className="ui-field">
            <span className="ui-field-label">Fleet Security Passphrase</span>
            <input
              type="password"
              placeholder="Enter authorization key…"
              value={passcode}
              onChange={(e) => setPasscode(e.target.value)}
              autoFocus
            />
          </label>
          {error && <div className="access-error-msg">{error}</div>}
          <button className="btn-primary btn-inline" type="submit">AUTHORIZE CONNECTION</button>
        </form>
      </div>
    </div>
  );
}
