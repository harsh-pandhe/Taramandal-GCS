// Fleet lifecycle state (issue #52) -> status dot colour.
const FLEET_STATE_DOT = {
  STAGED: 'grey',
  PREARM: 'orange',
  ARMED: 'orange',
  TAKEOFF: 'orange',
  FORMATION_HOLD: 'green',
  RUNNING: 'green',
  SHOW_COMPLETE: 'green',
  LANDING: 'red',
  RTL: 'red',
  ESTOP: 'red',
  DISARMED: 'grey',
};

/** Top bar: product mark, GCS telemetry link status, and fleet lifecycle state. */
export default function HeaderBar({ wsConnected, fleetState, session, onLogout, mode = 'SIMULATION', telemetryStatus = 'FRESH' }) {
  return (
    <header className="header-bar">
      <div className="logo-section">
        <h1>
          <svg className="drone-logo-svg" viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg" style={{ width: '38px', height: '38px', marginRight: '12px', filter: 'drop-shadow(0 0 8px var(--primary-glow))', verticalAlign: 'middle' }}>
            <circle cx="16" cy="16" r="8" stroke="var(--primary)" strokeWidth="1.5" strokeDasharray="4 2" />
            <circle cx="48" cy="16" r="8" stroke="var(--primary)" strokeWidth="1.5" strokeDasharray="4 2" />
            <circle cx="16" cy="48" r="8" stroke="var(--primary)" strokeWidth="1.5" strokeDasharray="4 2" />
            <circle cx="48" cy="48" r="8" stroke="var(--primary)" strokeWidth="1.5" strokeDasharray="4 2" />
            <path d="M10 16h12M16 10v12M42 16h12M48 10v12M10 48h12M16 42v12M42 48h12M48 42v12" stroke="var(--primary)" strokeWidth="1.2" />
            <path d="M21.6 21.6l20.8 20.8M42.4 21.6L21.6 42.4" stroke="var(--primary)" strokeWidth="2" />
            <circle cx="32" cy="32" r="6" fill="#0f172a" stroke="var(--primary)" strokeWidth="2" />
            <circle cx="32" cy="32" r="2" fill="var(--primary)" />
          </svg>
          TARAMANDAL GCS
        </h1>
      </div>
      <div className="system-status">
        <div className={`status-badge mode-badge ${mode === 'HARDWARE' ? 'hardware-mode' : 'simulation-mode'}`} data-testid="operating-mode">
          <span className={`dot ${mode === 'HARDWARE' ? 'orange' : 'blue'}`}></span>
          MODE: {mode} {telemetryStatus === 'STALE' ? '· STALE TELEMETRY' : ''}
        </div>
        <div className="status-badge">
          <span className={`dot ${wsConnected ? 'green' : 'red'}`}></span>
          GCS LINK: {wsConnected ? 'CONNECTED' : 'OFFLINE'}
        </div>
        {fleetState && (
          <div className="status-badge" title={fleetState.description} data-testid="fleet-state">
            <span className={`dot ${FLEET_STATE_DOT[fleetState.state] || 'grey'}`}></span>
            FLEET: {fleetState.state}
          </div>
        )}
        {session && (
          <div className="status-badge operator-badge" data-testid="operator">
            {session.operator} · {session.role}
            <button type="button" onClick={onLogout}>SIGN OUT</button>
          </div>
        )}
      </div>
    </header>
  );
}
