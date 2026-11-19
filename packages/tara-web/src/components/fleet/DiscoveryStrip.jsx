/**
 * Fleet discovery (issues #58, #304): which drones have announced themselves and which have gone quiet. STALE is shown in its
 * own words ("announcements stale"), never as the flight telemetry's ONLINE/OFFLINE, because the two are independent today: a
 * discovered drone is not connected to the flight path. Renders nothing when discovery is off.
 */
export default function DiscoveryStrip({ registry, stale = false, telemetryIds = [] }) {
  if (!registry) return null;
  const drones = registry.drones ?? [];
  const quiet = drones.filter((d) => d.state === 'STALE');
  const flying = new Set(telemetryIds.map(Number));
  const notInFlightPath = drones.filter((d) => !flying.has(d.drone_id)).length;
  const rejected = Object.values(registry.rejected ?? {}).reduce((a, b) => a + b, 0);
  return (
    <div className="fleet-health-strip" data-testid="discovery-strip" style={{ flexDirection: 'column', gap: '0.7rem' }}>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1.5rem' }}>
        <div className="fleet-stat">
          <span className="fleet-stat-label">Discovery</span>
          <span className="fleet-stat-value" data-testid="discovery-state" style={{ color: stale ? 'var(--warning)' : 'var(--success)' }}>
            {stale ? 'NO UPDATE' : 'ON'}
          </span>
        </div>
        <div className="fleet-stat">
          <span className="fleet-stat-label">Announcing</span>
          <span className="fleet-stat-value" data-testid="discovery-online">{registry.online}</span>
        </div>
        <div className="fleet-stat">
          <span className="fleet-stat-label">Announcements stale</span>
          <span className="fleet-stat-value" data-testid="discovery-stale" style={{ color: registry.stale > 0 ? 'var(--warning)' : undefined }}>
            {registry.stale}
          </span>
        </div>
        <div className="fleet-stat">
          <span className="fleet-stat-label">Not in the flight path</span>
          <span className="fleet-stat-value" data-testid="discovery-not-in-flight">{notInFlightPath}</span>
        </div>
        <div className="fleet-stat">
          <span className="fleet-stat-label">Rejected frames</span>
          <span className="fleet-stat-value">{rejected}</span>
        </div>
      </div>
      {quiet.length > 0 && (
        <div data-testid="discovery-stale-list" style={{ fontSize: '0.78rem', color: 'var(--warning)', fontFamily: 'var(--font-mono)' }}>
          {quiet.map((d) => (
            <span key={d.drone_id} style={{ marginRight: '1.2rem' }}>
              drone {String(d.drone_id).padStart(2, '0')} · quiet {d.age_s == null ? 'since the GCS started' : `${d.age_s} s`}
            </span>
          ))}
        </div>
      )}
      <div style={{ fontSize: '0.7rem', color: 'var(--text-faint)' }}>
        Announcements do not connect a drone: flight telemetry below is separate, and a stale announcement changes no flight decision.
      </div>
    </div>
  );
}
