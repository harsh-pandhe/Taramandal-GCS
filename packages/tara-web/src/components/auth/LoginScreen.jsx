import { useState } from 'react';
import { login } from '../../session';

/**
 * Operator sign-in (issue #64). Accounts are created on the GCS machine with
 * `python -m tara_gcs.auth add`, never from the browser.
 */
export default function LoginScreen() {
  const [operator, setOperator] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError('');
    try {
      await login(operator.trim(), passphrase);
    } catch (err) {
      setError(err.message === 'Failed to fetch' ? 'Cannot reach the GCS.' : err.message);
      setPassphrase('');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={submit}>
        <h1>TARAMANDAL GCS</h1>
        <p className="login-hint">Sign in as a GCS operator.</p>
        <label>
          Operator
          <input
            value={operator} onChange={(e) => setOperator(e.target.value)}
            autoComplete="username" autoFocus required
          />
        </label>
        <label>
          Passphrase
          <input
            type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)}
            autoComplete="current-password" required
          />
        </label>
        {error && <div className="login-error" role="alert">{error}</div>}
        <button type="submit" disabled={busy || !operator || !passphrase}>
          {busy ? 'SIGNING IN…' : 'SIGN IN'}
        </button>
        <p className="login-hint">
          No account? On the GCS machine run{' '}
          <code>python -m tara_gcs.auth add &lt;name&gt; --role flight_commander</code>
        </p>
      </form>
    </div>
  );
}
