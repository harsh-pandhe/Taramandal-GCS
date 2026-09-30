/** Fleet mission upload (issue #47): arming is gated on this server-side. */
export default function FleetMissionUpload({ missionStatus, isUploadingMission, uploadMissionToFleet }) {
  return (
    <div style={{ 
      background: 'rgba(255,255,255,0.02)', 
      padding: '0.4rem', 
      borderRadius: '4px', 
      marginBottom: '0.5rem', 
      border: '1px solid rgba(255,255,255,0.05)' 
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.3rem' }}>
        <span style={{ fontSize: '0.75rem', fontWeight: 600 }}>FLEET MISSION UPLOAD</span>
        <button 
          onClick={uploadMissionToFleet}
          disabled={isUploadingMission}
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
          {isUploadingMission ? 'Uploading...' : 'Upload'}
        </button>
      </div>
      
      {missionStatus && missionStatus.staged ? (
        <div style={{ fontSize: '0.72rem' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', marginBottom: '0.25rem' }}>
            <span>Status:</span>
            <span style={{ 
              color: missionStatus.launch_gate.ok ? 'var(--success)' : 'var(--danger)',
              fontWeight: 700
            }}>
              {missionStatus.launch_gate.ok
                ? `✓ ${missionStatus.verified_count}/${missionStatus.assigned_count} VERIFIED`
                : '✗ NOT READY TO ARM'}
            </span>
            <span style={{ color: 'var(--text-muted)', marginLeft: 'auto', fontFamily: 'monospace' }}>
              #{missionStatus.show_id.slice(0, 8)}
            </span>
          </div>
          {Object.keys(missionStatus.drones).map((id) => {
            const dMission = missionStatus.drones[id];
            return (
              <div key={id} title={dMission.msg} style={{ display: 'flex', justifyContent: 'space-between', padding: '0.15rem 0', borderBottom: '1px solid rgba(255,255,255,0.02)' }}>
                <span>Drone {id}:</span>
                <span style={{ color: dMission.status === 'VERIFIED' ? 'var(--success)' : 'var(--danger)' }}>
                  {dMission.status} (crc {dMission.crc32})
                </span>
              </div>
            );
          })}
          {!missionStatus.launch_gate.ok && (
            <div style={{ color: 'var(--danger)', marginTop: '0.25rem' }}>
              {missionStatus.launch_gate.reason}
            </div>
          )}
        </div>
      ) : (
        <div style={{ fontSize: '0.7rem', color: 'var(--text-muted)', fontStyle: 'italic' }}>
          Not uploaded to the fleet yet — arming is locked.
        </div>
      )}
    </div>
  );
}
