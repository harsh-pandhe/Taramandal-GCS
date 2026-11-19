const num = (v, unit = '') => (typeof v === 'number' && Number.isFinite(v) ? `${v}${unit}` : '—');

/**
 * The serial radio's link health (issue #68): connection, the modem's own RSSI / SNR / loss, the downlink's overruns and
 * the uplink's error counts. Renders nothing when the GCS has no radio configured. Values the radio has not reported show
 * "—", never a made-up number.
 */
export default function RadioLinkStrip({ link, stale = false }) {
  if (!link) return null;
  const { connected, downlink, uplink, radio } = link;
  const down = !connected;
  const color = down ? 'var(--danger)' : stale ? 'var(--warning)' : 'var(--success)';
  const state = down ? 'DOWN' : stale ? 'NO UPDATE' : 'UP';
  return (
    <div className="fleet-health-strip" data-testid="radio-link-strip" style={{ borderColor: down ? 'var(--danger)' : undefined }}>
      <div className="fleet-stat">
        <span className="fleet-stat-label">Radio link</span>
        <span className="fleet-stat-value" data-testid="radio-link-state" style={{ color }}>{state}</span>
      </div>
      {down && link.last_error && (
        <div className="fleet-stat" data-testid="radio-link-error">
          <span className="fleet-stat-label">Last error</span>
          <span className="fleet-stat-value">{link.last_error}</span>
        </div>
      )}
      <div className="fleet-stat">
        <span className="fleet-stat-label">RSSI</span>
        <span className="fleet-stat-value" data-testid="radio-rssi">{radio ? num(radio.rssi_dbm, ' dBm') : '—'}</span>
      </div>
      <div className="fleet-stat">
        <span className="fleet-stat-label">SNR</span>
        <span className="fleet-stat-value">{radio ? num(radio.snr_db, ' dB') : '—'}</span>
      </div>
      <div className="fleet-stat">
        <span className="fleet-stat-label">Packet loss</span>
        <span className="fleet-stat-value" data-testid="radio-loss">{radio ? num(radio.packet_loss_pct, ' %') : '—'}</span>
      </div>
      <div className="fleet-stat">
        <span className="fleet-stat-label">Downlink overruns</span>
        <span className="fleet-stat-value" data-testid="radio-overruns"
          style={{ color: downlink?.overruns > 0 ? 'var(--warning)' : undefined }}>{num(downlink?.overruns)}</span>
      </div>
      <div className="fleet-stat">
        <span className="fleet-stat-label">Uplink errors</span>
        <span className="fleet-stat-value">{num((uplink?.crc_errors ?? 0) + (uplink?.framing_errors ?? 0))}</span>
      </div>
      <div className="fleet-stat">
        <span className="fleet-stat-label">Drones reporting</span>
        <span className="fleet-stat-value">{num(uplink?.drones_reporting)}</span>
      </div>
      {!radio && !down && (
        <div className="fleet-stat" data-testid="radio-no-report">
          <span className="fleet-stat-label">Modem</span>
          <span className="fleet-stat-value" style={{ color: 'var(--text-muted)' }}>no health report yet</span>
        </div>
      )}
    </div>
  );
}
