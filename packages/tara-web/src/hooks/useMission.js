import { useEffect, useRef, useState } from 'react';
import { API_BASE } from '../config';
import { gcsFetch } from '../session';

// FastAPI puts errors in `detail`; a lifecycle 409 makes that an object whose
// `message` says why the command was refused and when it would be allowed.
export function apiErrorMessage(data, fallback) {
  const detail = data?.detail;
  if (detail && typeof detail === 'object') return detail.message || fallback;
  return detail || data?.message || fallback;
}

/**
 * Fleet commands and the trajectory workflow: signed upload (#49),
 * launch-geometry check, fleet mission upload (#47), start/stop show, plus
 * the default choreography preloaded on mount.
 *
 * @param {(msg: string) => void} setErrorMsg the dashboard's error banner.
 * @param {object|null} fleetState lifecycle state for command gating (#52, #96).
 */
export default function useMission(setErrorMsg, fleetState = null) {
  const [fileDetails, setFileDetails] = useState(null);
  const [trajSummary, setTrajSummary] = useState(null);
  const [trajectoryData, setTrajectoryData] = useState(null);
  // Two-tier geofence radii computed server-side from the loaded show's full
  // trajectory envelope (issue #53). null until a show that carries this
  // field has been loaded; EarthView/GeofenceRing fall back to the static
  // 30m default in that case (old exports, or before any show is loaded).
  const [geofenceRadii, setGeofenceRadii] = useState(null);
  const [isPlayingLocal, setIsPlaying] = useState(false);
  const isPlaying = fleetState ? fleetState.state === 'RUNNING' : isPlayingLocal;
  const canCommand = (command) => !fleetState || fleetState.allowed_commands.includes(command);

  const [flightMode, setFlightMode] = useState("LOCAL");
  const [triggerDelay, setTriggerDelay] = useState(3);
  const [verificationReport, setVerificationReport] = useState(null);
  const [isVerifying, setIsVerifying] = useState(false);
  // Onboard execution (issue #47): each drone holds its own mission and the
  // show starts on one broadcast trigger. "streamed" is the legacy 10Hz
  // unicast path, only for stock PX4 SITL.
  const [execution, setExecution] = useState('onboard');
  const [missionStatus, setMissionStatus] = useState(null);
  const [isUploadingMission, setIsUploadingMission] = useState(false);

  const fileInputRef = useRef(null);

  // Preload default trajectory on mount
  useEffect(() => {
    const loadDefaultTrajectory = async () => {
      try {
        const res = await fetch('/choreography.json');
        if (res.ok) {
          const data = await res.json();
          const trajectory_data = {};
          const summary = {};
          data.drones.forEach(drone => {
            trajectory_data[drone.id.toString()] = drone.waypoints;
            summary[drone.id.toString()] = {
              waypoints_count: drone.waypoints.length,
              duration_seconds: drone.waypoints.length > 0 ? drone.waypoints[drone.waypoints.length - 1].time : 0.0
            };
          });
          setTrajectoryData(trajectory_data);
          setTrajSummary(summary);
          setGeofenceRadii(data._geofence || null);
          setFileDetails({
            name: 'choreography.json (Preloaded)',
            size: 'Helix Pattern'
          });
        }
      } catch (err) {
        console.error('Failed to load default trajectory:', err);
      }
    };
    loadDefaultTrajectory();
  }, []);

  // Trigger POST Actions
  const triggerAction = async (endpoint) => {
    try {
      const res = await gcsFetch(`${API_BASE}${endpoint}`, { method: 'POST' });
      const data = await res.json();
      if (data.status !== 'success') {
        setErrorMsg(apiErrorMessage(data, 'Operation failed'));
      } else {
        setErrorMsg('');
      }
    } catch (err) {
      setErrorMsg(`API error: ${err.message}`);
    }
  };

  // Trajectory File Upload. The show file and its detached .sig.json
  // signature manifest are picked together (issue #49); the GCS refuses a
  // show without a valid signature unless it runs with --allow-unsigned.
  const handleFileUpload = async (event) => {
    const picked = Array.from(event.target.files);
    const signature = picked.find((f) => f.name.endsWith('.sig.json'));
    const file = picked.find((f) => f !== signature);
    if (!file) {
      setErrorMsg('Select the show file together with its .sig.json signature.');
      return;
    }

    setFileDetails({
      name: file.name,
      size: (file.size / 1024).toFixed(1) + ' KB'
    });

    const formData = new FormData();
    formData.append('file', file);
    if (signature) formData.append('signature', signature);

    try {
      const res = await gcsFetch(`${API_BASE}/api/upload-trajectory`, {
        method: 'POST',
        body: formData
      });
      const data = await res.json();
      
      if (res.ok && data.status === 'success') {
        setFileDetails((prev) => ({ ...prev, signature: data.signature }));
        setTrajSummary(data.drones_summary);
        setTrajectoryData(data.trajectory_data);
        setGeofenceRadii(data.geofence || null);
        setIsPlaying(false);
        setErrorMsg('');
        setMissionStatus(null);
        
        // Auto-run verification check immediately, then stage the show onto
        // the fleet — arming is refused until every drone holds a verified copy.
        setTimeout(() => {
          triggerVerifyLaunch();
          uploadMissionToFleet();
        }, 500);
      } else {
        setErrorMsg(apiErrorMessage(data, 'Failed to process trajectory'));
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
      const res = await gcsFetch(`${API_BASE}/api/verify-launch-geometry?tolerance_m=0.5`, {
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

  const uploadMissionToFleet = async () => {
    setIsUploadingMission(true);
    try {
      const res = await gcsFetch(`${API_BASE}/api/upload-mission`, { method: 'POST' });
      const data = await res.json();
      if (res.ok) {
        setMissionStatus(data);
      } else {
        setErrorMsg(data.detail || 'Failed to upload mission to fleet');
      }
    } catch (err) {
      setErrorMsg(`Mission upload error: ${err.message}`);
    } finally {
      setIsUploadingMission(false);
    }
  };

  const handleStartTrajectory = async () => {
    try {
      const triggerTimeEpoch = triggerDelay > 0 ? Date.now() + (triggerDelay * 1000) : 0.0;
      const res = await gcsFetch(`${API_BASE}/api/start-trajectory`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: flightMode,
          trigger_epoch_ms: triggerTimeEpoch,
          execution
        })
      });
      const data = await res.json();
      if (res.ok && data.status === 'success') {
        setIsPlaying(true);
        setErrorMsg('');
      } else {
        setErrorMsg(apiErrorMessage(data, 'Failed to start trajectory'));
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

  return {
    fileDetails, trajSummary, trajectoryData, geofenceRadii, isPlaying,
    flightMode, setFlightMode, triggerDelay, setTriggerDelay,
    verificationReport, isVerifying, fileInputRef,
    execution, setExecution, missionStatus, isUploadingMission,
    canCommand,
    triggerAction, handleFileUpload, triggerVerifyLaunch, uploadMissionToFleet,
    handleStartTrajectory, handleStopTrajectory, handleBoxClick,
  };
}
