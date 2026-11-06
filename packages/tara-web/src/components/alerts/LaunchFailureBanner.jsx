/**
 * Launch refusal banner (issue #122, PR #240).
 * Displays when the pre-show launch failed to get every planned drone to safe
 * formation altitude. The fleet is kept in TAKEOFF or DISARMED, and show start
 * is refused until the operator lands and resolves the pad/drone fault.
 */
export default function LaunchFailureBanner({ launchFailures }) {
  if (!launchFailures) return null;
  const entries = Object.entries(launchFailures);
  if (entries.length === 0) return null;

  return (
    <div
      role="alert"
      data-testid="launch-failures-banner"
      style={{
        background: 'rgba(239, 68, 68, 0.12)',
        border: '1px solid var(--danger)',
        borderLeft: '4px solid var(--danger)',
        padding: '0.85rem 1.1rem',
        borderRadius: 'var(--r)',
        color: '#fff',
        fontFamily: 'var(--font-mono)',
        fontSize: '0.85rem',
        marginBottom: '0.75rem',
        boxShadow: '0 4px 12px rgba(239, 68, 68, 0.15)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.35rem' }}>
        <span style={{ color: 'var(--danger)', fontSize: '1.05rem', fontWeight: 700 }}>⚠</span>
        <strong style={{ color: 'var(--danger)', letterSpacing: '0.04em' }}>
          LAUNCH INCOMPLETE — SHOW START REFUSED ({entries.length} DRONE{entries.length > 1 ? 'S' : ''})
        </strong>
      </div>
      <div style={{ color: 'var(--text-muted)', fontSize: '0.78rem', marginBottom: '0.5rem', lineHeight: 1.4 }}>
        The launch did not reach safe formation altitude for every planned vehicle. Land the fleet and clear the fault before relaunching.
      </div>
      <ul style={{ margin: 0, paddingLeft: '1.2rem', display: 'flex', flexDirection: 'column', gap: '0.25rem' }}>
        {entries.map(([id, reason]) => (
          <li key={id} style={{ color: '#fca5a5', fontSize: '0.82rem' }}>
            <span style={{ fontWeight: 600, color: '#fff' }}>Drone {id}:</span> {reason}
          </li>
        ))}
      </ul>
    </div>
  );
}
