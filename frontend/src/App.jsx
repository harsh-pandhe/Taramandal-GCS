import React, { useState, useEffect, useRef } from 'react';

const API_BASE = 'http://localhost:8000';
const WS_URL = 'ws://localhost:8000/ws/telemetry';

export default function App() {
  const [telemetry, setTelemetry] = useState({});
  const [wsConnected, setWsConnected] = useState(false);
  const [bypassChecklist, setBypassChecklist] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  
  // Trajectory states
  const [fileDetails, setFileDetails] = useState(null);
  const [trajSummary, setTrajSummary] = useState(null);
  const [isPlaying, setIsPlaying] = useState(false);
  
  const fileInputRef = useRef(null);

  // WebSocket Connection
  useEffect(() => {
    let ws;
    let reconnectTimer;

    function connect() {
      ws = new WebSocket(WS_URL);

      ws.onopen = () => {
        setWsConnected(true);
        setErrorMsg('');
        console.log('Telemetry WebSocket connected.');
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          setTelemetry(data);
        } catch (err) {
          console.error('Error parsing telemetry JSON:', err);
        }
      };

      ws.onclose = () => {
        setWsConnected(false);
        console.log('Telemetry WebSocket closed. Reconnecting in 2s...');
        reconnectTimer = setTimeout(connect, 2000);
      };

      ws.onerror = (err) => {
        console.error('WebSocket error:', err);
        ws.close();
      };
    }

    connect();

    return () => {
      if (ws) ws.close();
      clearTimeout(reconnectTimer);
    };
  }, []);

  // Calculate Checklist Evaluations
  const droneIds = Object.keys(telemetry);
  const connectedCount = droneIds.filter(id => telemetry[id].connected).length;
  
  const allConnected = droneIds.length > 0 && connectedCount === droneIds.length;
  
  const allGpsLocked = droneIds.length > 0 && droneIds.every(id => 
    !telemetry[id].connected || telemetry[id].gps_lock
  );
  
  const allBatterySafe = droneIds.length > 0 && droneIds.every(id => 
    !telemetry[id].connected || telemetry[id].battery_percent > 75
  );

  const checklistPassed = allConnected && allGpsLocked && allBatterySafe;
  const launchEnabled = checklistPassed || bypassChecklist;

  // Trigger POST Actions
  const triggerAction = async (endpoint) => {
    try {
      const res = await fetch(`${API_BASE}${endpoint}`, { method: 'POST' });
      const data = await res.json();
      if (data.status !== 'success') {
        setErrorMsg(data.message || 'Operation failed');
      } else {
        setErrorMsg('');
      }
    } catch (err) {
      setErrorMsg(`API error: ${err.message}`);
    }
  };

  // Trajectory File Upload
  const handleFileUpload = async (event) => {
    const file = event.target.files[0];
    if (!file) return;

    setFileDetails({
      name: file.name,
      size: (file.size / 1024).toFixed(1) + ' KB'
    });

    const formData = new FormData();
    formData.append('file', file);

    try {
      const res = await fetch(`${API_BASE}/api/upload-trajectory`, {
        method: 'POST',
        body: formData
      });
      const data = await res.json();
      
      if (res.ok && data.status === 'success') {
        setTrajSummary(data.drones_summary);
        setIsPlaying(true);
        setErrorMsg('');
      } else {
        setErrorMsg(data.detail || data.message || 'Failed to process trajectory');
        setTrajSummary(null);
      }
    } catch (err) {
      setErrorMsg(`Upload error: ${err.message}`);
      setTrajSummary(null);
    }
  };

  const handleStopTrajectory = async () => {
    await triggerAction('/api/stop-trajectory');
    setIsPlaying(false);
  };

  const handleBoxClick = () => {
    fileInputRef.current?.click();
  };

  // Battery bar color mapping
  const getBatteryColor = (percent) => {
    if (percent > 50) return '#00E676';
    if (percent > 25) return '#FFD600';
    return '#FF1744';
  };

  return (
    <div className="app-container">
      {/* Header bar */}
      <header className="header-bar">
        <div className="logo-section">
          <h1>
            <span className="logo-dot"></span>
            TARAMANDAL GCS
          </h1>
        </div>
        <div className="system-status">
          <div className="status-badge">
            <span className={`dot ${wsConnected ? 'green' : 'red'}`}></span>
            GCS LINK: {wsConnected ? 'CONNECTED' : 'OFFLINE'}
          </div>
        </div>
      </header>

      {errorMsg && (
        <div style={{
          background: 'rgba(255, 23, 68, 0.1)',
          border: '1px solid rgba(255, 23, 68, 0.3)',
          padding: '1rem',
          borderRadius: '12px',
          color: '#FF1744',
          fontWeight: 600,
          fontSize: '0.95rem'
        }}>
          ⚠️ {errorMsg}
        </div>
      )}

      {/* Main Grid Layout */}
      <div className="dashboard-grid">
        {/* Left side: Flight Controls and Fleet Status */}
        <main className="main-column">
          {/* Operations Panel */}
          <section className="gcs-panel">
            <h2 className="panel-header">
              FLIGHT OPERATIONS COMMAND
              <span style={{ fontSize: '0.85rem', fontWeight: 400, color: 'var(--text-muted)' }}>
                Active Drones: {connectedCount}/{droneIds.length || 3}
              </span>
            </h2>
            
            <div className="master-controls">
              <button 
                className="btn btn-launch" 
                onClick={() => triggerAction('/api/launch')}
                disabled={!launchEnabled || droneIds.length === 0}
              >
                🛫 START SEQUENTIAL TAKEOFF
              </button>
              
              <button 
                className="btn btn-land" 
                onClick={() => triggerAction('/api/land')}
              >
                🛬 EMER LAND
              </button>
              
              <button 
                className="btn btn-rtl" 
                onClick={() => triggerAction('/api/rtl')}
              >
                🚨 EMERGENCY RTL
              </button>
            </div>
            
            <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginTop: '0.75rem' }}>
              <input 
                type="checkbox" 
                id="bypass" 
                checked={bypassChecklist} 
                onChange={(e) => setBypassChecklist(e.target.checked)} 
                style={{ accentColor: 'var(--primary)', cursor: 'pointer' }}
              />
              <label htmlFor="bypass" style={{ fontSize: '0.85rem', color: 'var(--text-muted)', cursor: 'pointer' }}>
                Bypass pre-flight checklist restrictions (For testing/simulation)
              </label>
            </div>
          </section>

          {/* Fleet Status */}
          <section className="gcs-panel">
            <h2 className="panel-header">FLEET TELEMETRY</h2>
            
            <div className="fleet-grid">
              {droneIds.map((id) => {
                const drone = telemetry[id];
                const isConn = drone.connected;
                return (
                  <div key={id} className={`drone-card ${isConn ? '' : 'disconnected'}`}>
                    <div className="drone-card-header">
                      <div className="drone-id">
                        🛸 DRONE 0{id}
                        <span style={{ fontSize: '0.85rem', color: 'var(--text-muted)', fontWeight: 400 }}>
                          (Port {drone.port})
                        </span>
                      </div>
                      <span className={`drone-badge ${drone.flight_mode.toLowerCase()}`}>
                        {drone.flight_mode}
                      </span>
                    </div>

                    <div className="telemetry-subgrid">
                      <div className="telemetry-item">
                        <span className="telemetry-label">Status</span>
                        <span className="telemetry-value" style={{ color: isConn ? 'var(--success)' : 'var(--danger)' }}>
                          {isConn ? 'ONLINE' : 'OFFLINE'}
                        </span>
                      </div>
                      <div className="telemetry-item">
                        <span className="telemetry-label">Armed State</span>
                        <span className="telemetry-value" style={{ color: drone.armed ? 'var(--warning)' : 'var(--text-muted)' }}>
                          {drone.armed ? 'ARMED' : 'DISARMED'}
                        </span>
                      </div>
                      <div className="telemetry-item">
                        <span className="telemetry-label">GPS Satellites</span>
                        <span className="telemetry-value">
                          {isConn ? `${drone.satellites} SATS` : 'N/A'}
                        </span>
                      </div>
                      <div className="telemetry-item">
                        <span className="telemetry-label">GPS Lock</span>
                        <span className="telemetry-value" style={{ color: drone.gps_lock ? 'var(--success)' : 'var(--danger)' }}>
                          {isConn ? (drone.gps_lock ? 'FIX OK' : 'NO LOCK') : 'N/A'}
                        </span>
                      </div>
                    </div>

                    <div className="battery-container">
                      <div className="battery-header">
                        <span className="telemetry-label">Battery Level</span>
                        <span className="telemetry-value" style={{ fontSize: '0.9rem' }}>
                          {isConn ? `${drone.battery_percent}% (${drone.battery_voltage}V)` : 'N/A'}
                        </span>
                      </div>
                      <div className="battery-bar-bg">
                        <div 
                          className="battery-bar-fill" 
                          style={{ 
                            width: isConn ? `${Math.min(drone.battery_percent, 100)}%` : '0%',
                            backgroundColor: getBatteryColor(drone.battery_percent)
                          }}
                        />
                      </div>
                    </div>

                    <div className="coords-container">
                      <div className="coord-box">
                        <span className="coord-label">Local X (North)</span>
                        <span className="coord-val">{isConn ? `${drone.local_x}m` : '0.00m'}</span>
                      </div>
                      <div className="coord-box">
                        <span className="coord-label">Local Y (East)</span>
                        <span className="coord-val">{isConn ? `${drone.local_y}m` : '0.00m'}</span>
                      </div>
                      <div className="coord-box">
                        <span className="coord-label">Altitude (Z)</span>
                        <span className="coord-val" style={{ color: 'var(--success)' }}>
                          {isConn ? `${drone.local_z}m` : '0.00m'}
                        </span>
                      </div>
                    </div>
                  </div>
                );
              })}

              {droneIds.length === 0 && (
                <div style={{ gridColumn: '1 / -1', padding: '3rem', textAlign: 'center', color: 'var(--text-muted)' }}>
                  🛰️ Waiting for telemetry stream from PX4 SITL simulator...
                </div>
              )}
            </div>
          </section>
        </main>

        {/* Right side: Sidebar (Checklist & Trajectory Parser) */}
        <aside className="sidebar">
          {/* Preflight Checklist Panel */}
          <section className="gcs-panel">
            <h2 className="panel-header">PRE-FLIGHT CHECKLIST</h2>
            <div className="checklist-grid">
              <div className={`checklist-card ${allConnected ? 'valid' : ''}`}>
                <div className="checklist-title">
                  Connectivity
                  <span className={`check-indicator ${allConnected ? 'pass' : 'fail'}`}>
                    {allConnected ? '✓ PASS' : '✗ FAIL'}
                  </span>
                </div>
                <div className="checklist-item">
                  <span>Drones Connected:</span>
                  <span className="val">{connectedCount}/3</span>
                </div>
              </div>

              <div className={`checklist-card ${allGpsLocked ? 'valid' : ''}`}>
                <div className="checklist-title">
                  GPS Health
                  <span className={`check-indicator ${allGpsLocked ? 'pass' : 'fail'}`}>
                    {allGpsLocked ? '✓ PASS' : '✗ FAIL'}
                  </span>
                </div>
                <div className="checklist-item">
                  <span>Sats (Drone 0):</span>
                  <span className="val">{telemetry[0]?.satellites || 0}</span>
                </div>
                <div className="checklist-item">
                  <span>Sats (Drone 1):</span>
                  <span className="val">{telemetry[1]?.satellites || 0}</span>
                </div>
                <div className="checklist-item">
                  <span>Sats (Drone 2):</span>
                  <span className="val">{telemetry[2]?.satellites || 0}</span>
                </div>
              </div>

              <div className={`checklist-card ${allBatterySafe ? 'valid' : ''}`}>
                <div className="checklist-title">
                  Battery Safety
                  <span className={`check-indicator ${allBatterySafe ? 'pass' : 'fail'}`}>
                    {allBatterySafe ? '✓ PASS' : '✗ FAIL'}
                  </span>
                </div>
                <div className="checklist-item">
                  <span>Charge Threshold:</span>
                  <span className="val">&gt; 75%</span>
                </div>
                <div className="checklist-item">
                  <span>Status:</span>
                  <span className="val">{allBatterySafe ? 'Safe' : 'Insufficient'}</span>
                </div>
              </div>
            </div>
          </section>

          {/* Trajectory Planner Ingestion */}
          <section className="gcs-panel">
            <h2 className="panel-header">TRAJECTORY SWARM PLANNER</h2>
            
            <div className="uploader-box" onClick={handleBoxClick}>
              <div className="uploader-icon">📥</div>
              <div className="uploader-text">
                <strong>Click to ingest trajectory</strong>
                <br />
                Supports time-ordered .json / .csv
              </div>
              <input 
                type="file" 
                ref={fileInputRef} 
                onChange={handleFileUpload} 
                accept=".json,.csv" 
                className="uploader-file-input"
              />
            </div>

            {fileDetails && (
              <div className="file-info">
                <div className="file-info-header">
                  <span>📄 {fileDetails.name}</span>
                  <span>{fileDetails.size}</span>
                </div>
                {trajSummary ? (
                  <>
                    <div style={{ borderBottom: '1px solid rgba(255,255,255,0.05)', margin: '0.25rem 0' }}></div>
                    {Object.keys(trajSummary).map((id) => (
                      <div className="drone-stats-row" key={id}>
                        <span>Drone 0{id} Waypoints:</span>
                        <span>{trajSummary[id].waypoints_count} wps ({trajSummary[id].duration_seconds}s)</span>
                      </div>
                    ))}
                    <div className="trajectory-controls">
                      <button 
                        className="btn btn-stop" 
                        onClick={handleStopTrajectory}
                        style={{ flex: 1, padding: '0.6rem' }}
                      >
                        ⏹ Stop Trajectory
                      </button>
                    </div>
                  </>
                ) : (
                  <div style={{ color: 'var(--danger)', fontSize: '0.8rem' }}>Parsing...</div>
                )}
              </div>
            )}
          </section>
        </aside>
      </div>
    </div>
  );
}
