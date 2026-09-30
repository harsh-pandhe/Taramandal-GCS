/** Preflight Check Verification Panel: are the drones on their launch pads? */
export default function LaunchGeometryCheck({ verificationReport, isVerifying, triggerVerifyLaunch }) {
  return (
    <div style={{
      background: 'rgba(255,255,255,0.02)',
      padding: '0.4rem',
      borderRadius: '4px',
      marginBottom: '0.5rem',
      border: '1px solid rgba(255,255,255,0.05)'
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.3rem' }}>
        <span style={{ fontSize: '0.75rem', fontWeight: 600 }}>LAUNCH GEOMETRY CHECK</span>
        <button
          onClick={triggerVerifyLaunch}
          disabled={isVerifying}
          style={{
            padding: '0.15rem 0.35rem',
            fontSize: '0.65rem',
            background: 'rgba(56, 189, 248, 0.1)',
            border: '1px solid var(--primary)',
            color: 'var(--primary)',
            borderRadius: '3px',
            cursor: 'pointer'
          }}
        >
          {isVerifying ? 'Checking...' : 'Verify'}
        </button>
      </div>

      {verificationReport ? (
        <div style={{ fontSize: '0.72rem' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', marginBottom: '0.25rem' }}>
            <span>Status:</span>
            <span style={{
              color: verificationReport.all_passed ? 'var(--success)' : 'var(--danger)',
              fontWeight: 700,
              textShadow: verificationReport.all_passed ? '0 0 6px var(--success-glow)' : '0 0 6px rgba(255,23,68,0.3)'
            }}>
              {verificationReport.all_passed ? '✓ READY' : '✗ PLACEMENT ERROR'}
            </span>
          </div>
          {Object.keys(verificationReport.drones).map((id) => {
            const dReport = verificationReport.drones[id];
            return (
              <div key={id} style={{ display: 'flex', justifyContent: 'space-between', padding: '0.15rem 0', borderBottom: '1px solid rgba(255,255,255,0.02)' }}>
                <span>Drone {id}:</span>
                <span style={{ color: dReport.status === 'PASSED' ? 'var(--success)' : 'var(--danger)' }}>
                  {dReport.status} ({dReport.distance_error_m}m)
                </span>
              </div>
            );
          })}
        </div>
      ) : (
        <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
          No check performed yet.
        </div>
      )}
    </div>
  );
}
