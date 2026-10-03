import { useEffect, useState } from 'react';
import { API_BASE } from '../config';
import { gcsFetch } from '../session';

/**
 * Simulation Control Bar state (issue #2): polls the GCS for simulator
 * process status and launches/stops Gazebo, PX4 SITL and RViz, so running a
 * show never requires a terminal.
 *
 * @param {(msg: string) => void} setErrorMsg the dashboard's error banner.
 */
export default function useSimControl(setErrorMsg) {
  const [simStatus, setSimStatus] = useState(null);
  const [simBusy, setSimBusy] = useState(false);
  const [simDroneCount, setSimDroneCount] = useState(5);

  // Poll simulation process status. Deliberately slower than telemetry: this
  // reads the host's process table and psutil counters, which is expensive
  // relative to how fast "is Gazebo up" actually changes. The telemetry
  // WebSocket, not this, is what makes the fleet feel live.
  useEffect(() => {
    let cancelled = false;

    const poll = async () => {
      try {
        const res = await gcsFetch(`${API_BASE}/api/sim/status`);
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setSimStatus(data);
      } catch {
        // The GCS API being unreachable is already surfaced by the WebSocket
        // pill; a second error banner for the same cause would just be noise.
        if (!cancelled) setSimStatus(null);
      }
    };

    poll();
    const timer = setInterval(poll, 2000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  const handleSimLaunch = async () => {
    setSimBusy(true);
    try {
      // Real SITL is capped well below the top preset (see sim_manager's
      // PX4_MAX_DRONES), so large fleets go to the vectorized backend. The
      // operator picks a fleet size, not a backend — the choice of which
      // simulator can serve it is ours to make.
      const backend = simDroneCount > 50 ? 'sim' : 'px4';
      const res = await gcsFetch(`${API_BASE}/api/sim/launch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ num_drones: simDroneCount, backend, headless: true })
      });
      const data = await res.json();
      if (res.ok && data.status === 'success') {
        setSimStatus(data.sim);
        setErrorMsg('');
      } else {
        setErrorMsg(data.detail || data.message || 'Failed to launch simulation');
      }
    } catch (err) {
      setErrorMsg(`Sim launch error: ${err.message}`);
    } finally {
      setSimBusy(false);
    }
  };

  const handleRvizLaunch = async () => {
    setSimBusy(true);
    try {
      const res = await gcsFetch(`${API_BASE}/api/rviz/launch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ world: 'drone_show_field' })
      });
      const data = await res.json();
      if (res.ok && data.status === 'success') {
        setSimStatus(data.sim);
        setErrorMsg('');
      } else {
        setErrorMsg(data.detail || data.message || 'Failed to launch RViz');
      }
    } catch (err) {
      setErrorMsg(`RViz launch error: ${err.message}`);
    } finally {
      setSimBusy(false);
    }
  };

  const handleSimStop = async () => {
    setSimBusy(true);
    try {
      const res = await gcsFetch(`${API_BASE}/api/sim/stop`, { method: 'POST' });
      const data = await res.json();
      if (res.ok && data.status === 'success') {
        setSimStatus(data.sim);
        setErrorMsg('');
      } else {
        setErrorMsg(data.detail || data.message || 'Failed to stop simulation');
      }
    } catch (err) {
      setErrorMsg(`Sim stop error: ${err.message}`);
    } finally {
      setSimBusy(false);
    }
  };

  return {
    simStatus, simBusy, simDroneCount, setSimDroneCount,
    handleSimLaunch, handleRvizLaunch, handleSimStop,
  };
}
