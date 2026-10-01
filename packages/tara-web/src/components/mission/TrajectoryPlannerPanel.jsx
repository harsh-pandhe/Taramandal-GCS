import LaunchGeometryCheck from './LaunchGeometryCheck';
import FleetMissionUpload from './FleetMissionUpload';
import SwarmPathPreview from './SwarmPathPreview';

/**
 * Mission controls: ingest a (signed) trajectory, choose frame, execution and
 * trigger delay, check launch geometry, stage the missions onto the fleet,
 * start/stop the show, and preview the paths in 3D. State and handlers come
 * from useMission.
 */
export default function TrajectoryPlannerPanel({ mission, bypassChecklist }) {
  const {
    fileDetails, trajSummary, trajectoryData, geofenceRadii, isPlaying,
    flightMode, setFlightMode, triggerDelay, setTriggerDelay,
    verificationReport, isVerifying, fileInputRef,
    execution, setExecution, missionStatus, isUploadingMission,
    canCommand,
    handleFileUpload, triggerVerifyLaunch, uploadMissionToFleet,
    handleStartTrajectory, handleStopTrajectory, handleBoxClick,
  } = mission;

  return (
    <section className="gcs-panel">
      <h2 className="panel-header">TRAJECTORY SWARM PLANNER</h2>
      
      <div className="uploader-box" onClick={handleBoxClick}>
        <div className="uploader-text">
          <strong>Click to ingest trajectory</strong>
          <br />
          Supports time-ordered .json / .csv / .skyc
          <br />
          Select the show and its .sig.json together
        </div>
        <input 
          type="file" 
          ref={fileInputRef} 
          onChange={handleFileUpload} 
          accept=".json,.csv,.skyc" 
          multiple
          className="uploader-file-input"
        />
      </div>

      {fileDetails && (
        <div className="file-info">
          <div className="file-info-header">
            <span>
              {fileDetails.name}
              <span style={{
                color: isPlaying ? 'var(--success)' : 'var(--text-muted)',
                fontSize: '0.75rem',
                marginLeft: '0.5rem',
                fontWeight: 600,
                textShadow: isPlaying ? '0 0 8px var(--success-glow)' : 'none'
              }}>
                {isPlaying ? '• RUNNING' : '• STOPPED'}
              </span>
            </span>
            <span>{fileDetails.size}</span>
          </div>
          {fileDetails.signature && (
            <div style={{
              fontSize: '0.75rem',
              fontFamily: 'var(--font-mono)',
              color: fileDetails.signature.verified ? 'var(--success)' : 'var(--danger)'
            }}>
              {fileDetails.signature.verified
                ? `SIGNED BY ${fileDetails.signature.signer} (key ${fileDetails.signature.key_id})`
                : 'UNSIGNED: accepted only because the GCS runs with --allow-unsigned'}
            </div>
          )}
          {trajSummary ? (
            <>
              <div style={{ borderBottom: '1px solid rgba(255,255,255,0.05)', margin: '0.25rem 0' }}></div>
              
              {/* Operational Settings */}
              <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1.2fr 1fr', gap: '0.4rem', marginBottom: '0.5rem' }}>
                <div>
                  <label style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>MODE</label>
                  <select 
                    value={flightMode} 
                    onChange={(e) => setFlightMode(e.target.value)}
                    style={{
                      width: '100%',
                      background: 'rgba(0,0,0,0.3)',
                      border: '1px solid rgba(255,255,255,0.1)',
                      color: 'white',
                      padding: '0.2rem',
                      borderRadius: '4px',
                      fontSize: '0.75rem',
                      outline: 'none',
                      marginTop: '0.15rem'
                    }}
                  >
                    <option value="LOCAL">LOCAL (NED)</option>
                    <option value="GLOBAL">GLOBAL (GPS)</option>
                  </select>
                </div>
                <div>
                  <label style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>EXECUTION</label>
                  <select 
                    value={execution} 
                    onChange={(e) => setExecution(e.target.value)}
                    style={{
                      width: '100%',
                      background: 'rgba(0,0,0,0.3)',
                      border: '1px solid rgba(255,255,255,0.1)',
                      color: 'white',
                      padding: '0.2rem',
                      borderRadius: '4px',
                      fontSize: '0.75rem',
                      outline: 'none',
                      marginTop: '0.15rem'
                    }}
                  >
                    <option value="onboard">ONBOARD</option>
                    <option value="streamed">STREAMED (SITL)</option>
                  </select>
                </div>
                <div>
                  <label style={{ fontSize: '0.7rem', color: 'var(--text-muted)' }}>DELAY (S)</label>
                  <input 
                    type="number" 
                    min="0" 
                    max="30"
                    value={triggerDelay} 
                    onChange={(e) => setTriggerDelay(parseInt(e.target.value) || 0)}
                    style={{
                      width: '100%',
                      background: 'rgba(0,0,0,0.3)',
                      border: '1px solid rgba(255,255,255,0.1)',
                      color: 'white',
                      padding: '0.2rem',
                      borderRadius: '4px',
                      fontSize: '0.75rem',
                      outline: 'none',
                      marginTop: '0.15rem'
                    }}
                  />
                </div>
              </div>

              <LaunchGeometryCheck
                verificationReport={verificationReport}
                isVerifying={isVerifying}
                triggerVerifyLaunch={triggerVerifyLaunch}
              />

              <FleetMissionUpload
                missionStatus={missionStatus}
                isUploadingMission={isUploadingMission}
                uploadMissionToFleet={uploadMissionToFleet}
              />

              <div className="trajectory-controls" style={{ display: 'flex', gap: '0.4rem', marginTop: '0.4rem' }}>
                <button 
                  className="btn btn-launch" 
                  onClick={handleStartTrajectory}
                  disabled={
                    isPlaying
                    || (canCommand && !canCommand('start_trajectory'))
                    || (execution === 'onboard' && !(missionStatus && missionStatus.all_verified))
                    || (!bypassChecklist && (!verificationReport || !verificationReport.all_passed))
                  }
                  style={{ flex: 1.5, padding: '0.5rem', background: isPlaying ? 'var(--text-muted)' : 'var(--success)', cursor: 'pointer' }}
                >
                  START SHOW
                </button>
                <button 
                  className="btn btn-stop" 
                  onClick={handleStopTrajectory}
                  disabled={!isPlaying || (canCommand && !canCommand('stop_trajectory'))}
                  style={{ flex: 1, padding: '0.5rem', cursor: 'pointer' }}
                >
                  ⏹ STOP SHOW
                </button>
              </div>
              {/* Visual 2D swarm path preview */}
              <SwarmPathPreview trajectoryData={trajectoryData} geofenceRadiusM={geofenceRadii?.hard_geofence_radius_m ?? 30.0} />
            </>
          ) : (
            <div style={{ color: 'var(--danger)', fontSize: '0.8rem' }}>Parsing...</div>
          )}
        </div>
      )}
    </section>
  );
}
