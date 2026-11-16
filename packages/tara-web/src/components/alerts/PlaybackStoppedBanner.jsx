/**
 * The fleet state reads RUNNING but the GCS is not playing the show (issue #299).
 *
 * A fleet-wide LAND, RTL or emergency stop stops the playback before the vehicles answer. If every vehicle refuses it,
 * the fleet state is rightly not advanced to LANDING / RTL / ESTOP, and nothing moves it out of RUNNING either, so the
 * vehicles are neither flying the show nor landing while the label still says RUNNING. This says so. The state machine
 * is unchanged; the flag is derived on the GCS (`playback_stopped`).
 */
export default function PlaybackStoppedBanner({ stopped }) {
  if (!stopped) return null;

  return (
    <div
      role="alert"
      data-testid="playback-stopped-banner"
      style={{
        background: 'rgba(245, 158, 11, 0.12)',
        border: '1px solid var(--warning)',
        borderLeft: '4px solid var(--warning)',
        padding: '0.85rem 1.1rem',
        borderRadius: 'var(--r)',
        color: '#fff',
        fontFamily: 'var(--font-mono)',
        fontSize: '0.85rem',
        marginBottom: '0.75rem',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.35rem' }}>
        <span style={{ color: 'var(--warning)', fontSize: '1.05rem', fontWeight: 700 }}>⚠</span>
        <strong style={{ color: 'var(--warning)', letterSpacing: '0.04em' }}>
          SHOW STOPPED — THE FLEET STATE STILL READS RUNNING
        </strong>
      </div>
      <div style={{ color: 'var(--text-muted)', fontSize: '0.82rem', lineHeight: 1.45 }}>
        The GCS is no longer playing the show. A land, RTL or emergency stop stops it before the vehicles answer; if every
        vehicle refused the command, they are neither flying the show nor landing. Check the vehicles, and take over
        manually or send the command again.
      </div>
    </div>
  );
}
