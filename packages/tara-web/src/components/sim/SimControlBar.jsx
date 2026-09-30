/**
 * Simulation Control Bar — start/stop the simulator itself, so a show
 * director never needs a terminal or `tara up`. State and handlers come from
 * useSimControl.
 */
export default function SimControlBar({
  simStatus, simBusy, simDroneCount, setSimDroneCount,
  handleSimLaunch, handleRvizLaunch, handleSimStop,
}) {
  // The bar has to render before any status has been fetched, so every field
  // is defaulted rather than guarded at each use site.
  const simRunning = Boolean(simStatus?.running);
  const simHeartbeat = simStatus?.heartbeat || { alive: false, connected: 0, expected: 0 };
  // Expected node count comes from whichever source actually knows: the running
  // fleet's own port count once it is up, the launch request before telemetry
  // arrives, and the slider before anything is running at all.
  const simNodeTotal = simHeartbeat.expected || simStatus?.num_drones || simDroneCount;
  const simCpu = simStatus?.cpu_percent || 0;

  return (
    <section className="sim-control-bar">
      <div className="sim-pills">
        <div className="status-badge">
          <span className={`dot ${simRunning ? 'green' : 'red'}`}></span>
          SITL: {simRunning ? 'RUNNING' : 'INACTIVE'}
        </div>
        <div className="status-badge">
          <span className={`dot ${simHeartbeat.alive ? 'green' : 'red'}`}></span>
          NODES: {simHeartbeat.connected ?? 0}/{simNodeTotal}
        </div>
        <div className="status-badge">
          <span className={`dot ${simCpu > 85 ? 'red' : simCpu > 60 ? 'orange' : 'green'}`}></span>
          CPU: {simCpu.toFixed(0)}%
        </div>
        {simStatus?.memory_mb > 0 && (
          <div className="status-badge">RAM: {(simStatus.memory_mb / 1024).toFixed(1)} GB</div>
        )}
        {simStatus?.rviz_running && (
          <div className="status-badge">
            <span className="dot green"></span>
            RVIZ: UP
          </div>
        )}
      </div>

      <div className="sim-actions">
        <label className="sim-slider-label" htmlFor="sim-drone-count">
          FLEET SIZE
          <select
            id="sim-drone-count"
            className="sim-select"
            value={simDroneCount}
            onChange={(e) => setSimDroneCount(Number(e.target.value))}
            disabled={simRunning || simBusy}
          >
            {[5, 100, 500].map((n) => (
              <option key={n} value={n}>{n} drones</option>
            ))}
          </select>
        </label>

        <button
          className="btn btn-launch"
          onClick={handleSimLaunch}
          disabled={simRunning || simBusy}
        >
          🚀 LAUNCH GAZEBO SITL
        </button>

        <button
          className="btn btn-stop"
          onClick={handleRvizLaunch}
          disabled={!simRunning || simBusy || simStatus?.rviz_running}
          title={simRunning ? 'Open RViz2 with the drone-swarm display' : 'Launch the simulation first'}
        >
          👁️ LAUNCH RVIZ
        </button>

        <button
          className="btn btn-rtl"
          onClick={handleSimStop}
          disabled={!simRunning || simBusy}
        >
          🛑 TERMINATE SIM
        </button>
      </div>
    </section>
  );
}
