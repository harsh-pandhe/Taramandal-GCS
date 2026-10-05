import { useEffect, useState } from 'react';
import { Download, Globe, Activity, ShieldCheck, ShieldAlert, LogIn, LogOut } from 'lucide-react';
import { NumberField } from '../common/index.jsx';
import {
  getSigningStatus, getGcsSession, gcsLogin, gcsLogout, onGcsSessionChange,
} from '../../engine/api.js';

/** Which key signs exports (issue #49); the private key never leaves the backend. */
function SigningStatus() {
  const [status, setStatus] = useState(null);
  useEffect(() => {
    let live = true;
    getSigningStatus()
      .then((s) => { if (live) setStatus(s); })
      .catch(() => { if (live) setStatus({ ready: false, error: 'Could not reach the Studio backend.' }); });
    return () => { live = false; };
  }, []);

  if (!status) return null;
  if (status.ready) {
    return (
      <div className="signing-status ok" data-testid="signing-status">
        <ShieldCheck size={14} />
        <span>JSON/CSV exports are signed with key <code>{status.key_id}</code>.</span>
      </div>
    );
  }
  return (
    <div className="signing-status warn" data-testid="signing-status">
      <ShieldAlert size={14} />
      <span>
        Exports are <strong>not signed</strong>; the GCS refuses unsigned shows unless it runs
        with <code>--allow-unsigned</code>. {status.error}
      </span>
    </div>
  );
}

/** GCS operator sign-in for uploads (issue #64): CHOREOGRAPHER or above. */
function GcsSignIn() {
  const [session, setSession] = useState(getGcsSession);
  const [operator, setOperator] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => onGcsSessionChange(() => setSession(getGcsSession())), []);

  if (session) {
    return (
      <div className="gcs-session" data-testid="gcs-session">
        <span>GCS: <strong>{session.operator}</strong> · {session.role}</span>
        <button type="button" className="btn-secondary btn-sm" onClick={gcsLogout}>
          <LogOut size={12} /> Sign out
        </button>
      </div>
    );
  }
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await gcsLogin(operator.trim(), passphrase);
      setPassphrase('');
    } catch (err) {
      setError(err.message === 'Failed to fetch' ? 'Cannot reach the GCS.' : err.message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="gcs-signin" onSubmit={submit} aria-label="Sign in to GCS">
      <input
        value={operator} onChange={(e) => setOperator(e.target.value)}
        placeholder="GCS operator" aria-label="GCS operator" autoComplete="username"
      />
      <input
        type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)}
        placeholder="Passphrase" aria-label="GCS passphrase" autoComplete="current-password"
      />
      <button type="submit" className="btn-secondary btn-sm" disabled={busy || !operator || !passphrase}>
        <LogIn size={12} /> Sign in to GCS
      </button>
      {error && <span className="gcs-signin-error" role="alert">{error}</span>}
    </form>
  );
}

/** Venue georeference plus export / GCS upload actions. */
export default function TransmitPanel({
  homeLat, homeLon, onHomeLat, onHomeLon,
  onExport, onSendToGCS, sending, hasShow,
}) {
  return (
    <div className="glass-panel-inner">
      <h4 className="section-subtitle">Venue Calibration</h4>
      <div className="form-row">
        <NumberField label="Venue Lat" value={homeLat} onChange={onHomeLat} step={0.0001} min={-90} max={90} />
        <NumberField label="Venue Lon" value={homeLon} onChange={onHomeLon} step={0.0001} min={-180} max={180} />
      </div>

      <h4 className="section-subtitle">Export &amp; Transmit</h4>
      <SigningStatus />
      <div className="form-row">
        <button type="button" className="btn-secondary btn-inline" onClick={() => onExport('json')} disabled={!hasShow}>
          <Download size={14} /> JSON
        </button>
        <button type="button" className="btn-secondary btn-inline" onClick={() => onExport('csv')} disabled={!hasShow}>
          <Download size={14} /> CSV
        </button>
      </div>

      <button type="button" className="btn-secondary btn-inline" onClick={() => onExport('tara')} disabled={!hasShow}>
        Compile .tara (binary)
      </button>

      <button type="button" className="btn-primary btn-inline" onClick={() => onExport('kml')} disabled={!hasShow}>
        <Globe size={15} /> Export Google Earth KML
      </button>

      <GcsSignIn />
      <button type="button" className="btn-primary btn-inline btn-gcs" onClick={onSendToGCS} disabled={sending || !hasShow}>
        <Activity size={15} /> {sending ? 'Sending…' : 'Transmit to GCS Fleet'}
      </button>

      <p className="panel-hint">
        Exports carry baked LED colour per waypoint, so the light choreography
        travels with the trajectory. A signed export downloads as two files,
        the show and its <code>.sig.json</code>; transmit sends both.
      </p>
    </div>
  );
}
