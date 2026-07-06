import React, { useState, useEffect, useRef, useMemo } from 'react';
import { Canvas } from '@react-three/fiber';
import { OrbitControls, Line, Text, Grid } from '@react-three/drei';

const API_BASE = import.meta.env.VITE_API_BASE || `http://${window.location.hostname || 'localhost'}:8000`;
const WS_URL = import.meta.env.VITE_WS_URL || `ws://${window.location.hostname || 'localhost'}:8000/ws/telemetry`;

export default function App() {
  const [telemetry, setTelemetry] = useState({});
  const [wsConnected, setWsConnected] = useState(false);
  const [bypassChecklist, setBypassChecklist] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  
  // Trajectory states
  const [fileDetails, setFileDetails] = useState(null);
  const [trajSummary, setTrajSummary] = useState(null);
  const [trajectoryData, setTrajectoryData] = useState(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [flightMode, setFlightMode] = useState("LOCAL");
  const [triggerDelay, setTriggerDelay] = useState(3);
  const [verificationReport, setVerificationReport] = useState(null);
  const [isVerifying, setIsVerifying] = useState(false);
  
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
  const totalDrones = droneIds.length || 3;
  
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
        setTrajectoryData(data.trajectory_data);
        setIsPlaying(false);
        setErrorMsg('');
        
        // Auto-run verification check immediately
        setTimeout(() => {
          triggerVerifyLaunch();
        }, 500);
      } else {
        setErrorMsg(data.detail || data.message || 'Failed to process trajectory');
        setTrajSummary(null);
      }
    } catch (err) {
      setErrorMsg(`Upload error: ${err.message}`);
      setTrajSummary(null);
    }
  };

  const triggerVerifyLaunch = async () => {
    setIsVerifying(true);
    try {
      const res = await fetch(`${API_BASE}/api/verify-launch-geometry?tolerance_m=0.5`, {
        method: 'POST'
      });
      const data = await res.json();
      if (res.ok) {
        setVerificationReport(data);
      } else {
        setErrorMsg(data.detail || 'Failed to verify launch geometry');
      }
    } catch (err) {
      setErrorMsg(`Verification error: ${err.message}`);
    } finally {
      setIsVerifying(false);
    }
  };

  const handleStartTrajectory = async () => {
    try {
      const triggerTimeEpoch = triggerDelay > 0 ? Date.now() + (triggerDelay * 1000) : 0.0;
      const res = await fetch(`${API_BASE}/api/start-trajectory`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: flightMode,
          trigger_epoch_ms: triggerTimeEpoch
        })
      });
      const data = await res.json();
      if (res.ok && data.status === 'success') {
        setIsPlaying(true);
        setErrorMsg('');
      } else {
        setErrorMsg(data.detail || data.message || 'Failed to start trajectory');
      }
    } catch (err) {
      setErrorMsg(`Start error: ${err.message}`);
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
                      <div className="telemetry-item">
                        <span className="telemetry-label">RTK Status</span>
                        <span className="telemetry-value" style={{
                          color: isConn && (drone.gps_fix_type === 'RTK_FIXED' || drone.gps_fix_type === 'RTK_FLOAT') ? 'var(--primary)' : 'var(--text-muted)'
                        }}>
                          {isConn ? (drone.gps_fix_type || 'N/A') : 'N/A'}
                        </span>
                      </div>
                      <div className="telemetry-item">
                        <span className="telemetry-label">Latency</span>
                        <span className="telemetry-value" style={{
                          color: isConn ? (drone.comm_latency_ms < 50 ? 'var(--success)' : drone.comm_latency_ms < 150 ? 'var(--warning)' : 'var(--danger)') : 'var(--text-muted)'
                        }}>
                          {isConn ? `${drone.comm_latency_ms || 0.0} ms` : 'N/A'}
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
                  <span className="val">{connectedCount}/{totalDrones}</span>
                </div>
              </div>

              <div className={`checklist-card ${allGpsLocked ? 'valid' : ''}`}>
                <div className="checklist-title">
                  GPS Health
                  <span className={`check-indicator ${allGpsLocked ? 'pass' : 'fail'}`}>
                    {allGpsLocked ? '✓ PASS' : '✗ FAIL'}
                  </span>
                </div>
                {droneIds.length > 0 ? (
                  droneIds.map((id) => (
                    <div className="checklist-item" key={id}>
                      <span>Sats (Drone 0{id}):</span>
                      <span className="val">{telemetry[id]?.satellites || 0}</span>
                    </div>
                  ))
                ) : (
                  <>
                    <div className="checklist-item">
                      <span>Sats (Drone 00):</span>
                      <span className="val">0</span>
                    </div>
                    <div className="checklist-item">
                      <span>Sats (Drone 01):</span>
                      <span className="val">0</span>
                    </div>
                    <div className="checklist-item">
                      <span>Sats (Drone 02):</span>
                      <span className="val">0</span>
                    </div>
                  </>
                )}
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
                accept=".json,.csv,.skyc" 
                className="uploader-file-input"
              />
            </div>

            {fileDetails && (
              <div className="file-info">
                <div className="file-info-header">
                  <span>
                    📄 {fileDetails.name}
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
                {trajSummary ? (
                  <>
                    <div style={{ borderBottom: '1px solid rgba(255,255,255,0.05)', margin: '0.25rem 0' }}></div>
                    
                    {/* Operational Settings */}
                    <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1fr', gap: '0.4rem', marginBottom: '0.5rem' }}>
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

                    {/* Preflight Check Verification Panel */}
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
                          {isVerifying ? 'Checking...' : '🔄 Verify'}
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

                    <div className="trajectory-controls" style={{ display: 'flex', gap: '0.4rem', marginTop: '0.4rem' }}>
                      <button 
                        className="btn btn-launch" 
                        onClick={handleStartTrajectory}
                        disabled={isPlaying || (!bypassChecklist && (!verificationReport || !verificationReport.all_passed))}
                        style={{ flex: 1.5, padding: '0.5rem', background: isPlaying ? 'var(--text-muted)' : 'var(--success)', cursor: 'pointer' }}
                      >
                        🚀 START SHOW
                      </button>
                      <button 
                        className="btn btn-stop" 
                        onClick={handleStopTrajectory}
                        disabled={!isPlaying}
                        style={{ flex: 1, padding: '0.5rem', cursor: 'pointer' }}
                      >
                        ⏹ STOP SHOW
                      </button>
                    </div>
                    {/* Visual 2D swarm path preview */}
                    <SwarmCanvasPreview trajectoryData={trajectoryData} />
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


// ─── 3D Swarm Path Previewer (Three.js via react-three-fiber) ────────────────

const DRONE_COLORS = ['#00E5FF', '#FF00FF', '#FF9100', '#00E676', '#FFFF00'];

function DronePathLine({ waypoints, color }) {
  // NED → Three.js axes: X=East(y_NED), Y=Up(-z_NED), Z=North(x_NED)
  const points = useMemo(() =>
    waypoints.map(wp => [wp.y, -wp.z, wp.x]),
    [waypoints]
  );
  if (points.length < 2) return null;
  return (
    <>
      <Line points={points} color={color} lineWidth={2.5} />
      {points.map((pt, i) => (
        <mesh key={i} position={pt}>
          <sphereGeometry args={[i === 0 || i === points.length - 1 ? 0.28 : 0.13, 10, 10]} />
          <meshStandardMaterial color={color} emissive={color} emissiveIntensity={i === 0 ? 1.5 : 0.5} />
        </mesh>
      ))}
    </>
  );
}

function GeofenceRing({ radius = 30 }) {
  const pts = useMemo(() => {
    const arr = [];
    for (let i = 0; i <= 80; i++) {
      const a = (i / 80) * Math.PI * 2;
      arr.push([Math.sin(a) * radius, 0.05, Math.cos(a) * radius]);
    }
    return arr;
  }, [radius]);
  return <Line points={pts} color="#FF3D00" lineWidth={1.2} />;
}

function SwarmCanvasPreview({ trajectoryData }) {
  const droneEntries = useMemo(() => {
    if (!trajectoryData) return [];
    return Object.keys(trajectoryData)
      .map((id, idx) => ({ id, waypoints: trajectoryData[id], color: DRONE_COLORS[idx % DRONE_COLORS.length] }))
      .filter(d => d.waypoints && d.waypoints.length > 0);
  }, [trajectoryData]);

  const cameraPos = useMemo(() => {
    if (!trajectoryData) return [15, 12, 15];
    let maxR = 5;
    Object.values(trajectoryData).forEach(wps =>
      wps.forEach(wp => {
        const r = Math.sqrt(wp.x ** 2 + wp.y ** 2 + wp.z ** 2);
        if (r > maxR) maxR = r;
      })
    );
    const d = Math.min(maxR * 2.2, 45);
    return [d, d * 0.7, d];
  }, [trajectoryData]);

  return (
    <div style={{ marginTop: '1rem', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '12px', background: '#080d18', overflow: 'hidden' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '0.55rem 0.75rem', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
        <span style={{ fontSize: '0.74rem', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>🛸 3D Swarm Path Preview</span>
        <span style={{ fontSize: '0.68rem', color: 'rgba(255,255,255,0.2)' }}>Drag · Orbit · Scroll to zoom</span>
      </div>

      {!trajectoryData ? (
        <div style={{ height: '220px', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'rgba(255,255,255,0.18)', fontSize: '0.8rem', flexDirection: 'column', gap: '0.4rem' }}>
          <span style={{ fontSize: '1.5rem' }}>📐</span>
          Upload a trajectory to preview paths in 3D
        </div>
      ) : (
        <div style={{ height: '260px' }}>
          <Canvas camera={{ position: cameraPos, fov: 48 }} style={{ background: 'transparent' }}>
            <ambientLight intensity={0.4} />
            <pointLight position={[10, 20, 10]} intensity={1.4} />

            <Grid
              args={[80, 80]}
              cellSize={5}
              cellThickness={0.4}
              cellColor="#111827"
              sectionSize={10}
              sectionThickness={1}
              sectionColor="#1e293b"
              fadeDistance={70}
              position={[0, 0, 0]}
            />

            {/* 30m geofence ring */}
            <GeofenceRing radius={30} />

            {/* Origin pad */}
            <mesh position={[0, 0.02, 0]}>
              <cylinderGeometry args={[0.7, 0.7, 0.05, 32]} />
              <meshStandardMaterial color="#00E5FF" emissive="#00E5FF" emissiveIntensity={0.5} />
            </mesh>

            {droneEntries.map(({ id, waypoints, color }) => (
              <DronePathLine key={id} waypoints={waypoints} color={color} />
            ))}

            {droneEntries.map(({ id, waypoints, color }) => {
              const w0 = waypoints[0];
              return (
                <Text key={`lbl-${id}`} position={[w0.y, -w0.z + 1.0, w0.x]} fontSize={0.45} color={color} anchorX="center">
                  {`D${id}`}
                </Text>
              );
            })}

            <OrbitControls makeDefault enableDamping dampingFactor={0.06} />
          </Canvas>
        </div>
      )}

      {trajectoryData && (
        <div style={{ display: 'flex', gap: '0.6rem', padding: '0.45rem 0.75rem', borderTop: '1px solid rgba(255,255,255,0.04)', flexWrap: 'wrap', alignItems: 'center' }}>
          {droneEntries.map(({ id, color }) => (
            <div key={id} style={{ display: 'flex', alignItems: 'center', gap: '0.3rem' }}>
              <div style={{ width: 8, height: 8, borderRadius: '50%', background: color, boxShadow: `0 0 6px ${color}` }} />
              <span style={{ fontSize: '0.68rem', color: 'rgba(255,255,255,0.45)' }}>Drone {id}</span>
            </div>
          ))}
          <div style={{ display: 'flex', alignItems: 'center', gap: '0.3rem', marginLeft: 'auto' }}>
            <div style={{ width: 14, height: 2, background: '#FF3D00', opacity: 0.7 }} />
            <span style={{ fontSize: '0.68rem', color: 'rgba(255,255,255,0.28)' }}>30m Geofence</span>
          </div>
        </div>
      )}
    </div>
  );
}

