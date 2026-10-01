import asyncio
import logging
import os
from pathlib import Path
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from tara_gcs.drone_manager import DroneManager, LaunchGateError
from tara_gcs.trajectory_parser import parse_trajectory_bytes
from tara_gcs.telemetry_codec import FAST_HZ, SLOW_HZ, encode_fast_frame, encode_slow_frame
from tara_gcs.sim_manager import SimLaunchError, SimManager
from tara_gcs.show_auth import load_trust_policy
from tara_core.signing import MAX_MANIFEST_BYTES, SignatureError
from tara_gcs.preflight import AuditLog, ConfirmationError, PreArmChecklistEngine, load_safety_config

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TaramandalGCS")

app = FastAPI(title="Taramandal Ground Control Station API")

# Enable CORS for frontend connectivity
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", 
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect to simulation drones dynamically (defaulting to 3)
NUM_DRONES = int(os.environ.get("NUM_DRONES", 3))
ports = [14540 + i for i in range(NUM_DRONES)]
# "px4" (default) is real MAVSDK/PX4 SITL; "sim" is the vectorized tara_sim
# fleet, which scales past the ~10-drone ceiling real SITL hits on one
# machine. Port values are irrelevant for "sim" — only the count matters.
VEHICLE_BACKEND = os.environ.get("VEHICLE_BACKEND", "px4")
kwargs = {}
if os.environ.get("GCS_STATE_PATH"):
    kwargs["state_path"] = os.environ["GCS_STATE_PATH"]
drone_manager = DroneManager(ports=ports, backend=VEHICLE_BACKEND, **kwargs)

# Owns the Gazebo/PX4/RViz processes the dashboard's Simulation Control Bar
# starts, so an operator never needs a terminal. Repo root is resolved from
# this file's location (packages/tara-gcs/tara_gcs/main.py -> three levels up)
# rather than the process CWD, which is whatever directory uvicorn happened to
# be started from.
REPO_ROOT = Path(__file__).resolve().parents[3]
sim_manager = SimManager(repo_root=REPO_ROOT)

# Trusted show-signing keys come from tara.toml's [security] table (issue #49).
# GCS_CONFIG_PATH points at a different tara.toml, e.g. a test's temp one.
_tara_toml_path = Path(os.environ.get("GCS_CONFIG_PATH") or REPO_ROOT / "tara.toml")
trust_policy = load_trust_policy(
    _tara_toml_path,
    allow_unsigned=os.environ.get("GCS_ALLOW_UNSIGNED", "").lower() in ("1", "true", "yes"),
)

# Pre-arm checklist engine (issue #48). The operational profile comes from
# tara.toml's [safety] table (or TARA_OPERATIONAL_PROFILE), never from the API,
# so nothing a browser sends can relax the gate a GCS was started with.
safety_config = load_safety_config(_tara_toml_path)
audit_kwargs = {"path": os.environ["GCS_AUDIT_PATH"]} if os.environ.get("GCS_AUDIT_PATH") else {}
preflight_engine = PreArmChecklistEngine(
    profile=safety_config.profile,
    battery_cells=safety_config.battery_cells,
    audit_log=AuditLog(**audit_kwargs),
)
logger.info(f"Pre-arm safety profile: {safety_config.profile.value}")

@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Ground Control Station...")

    # If a previous run left a saved show on disk (crash or restart mid-show,
    # issue #4), surface it for the operator to resume/discard — never applied
    # automatically, and never arms or launches anything on its own.
    saved_state = drone_manager.load_pending_resume_state()
    if saved_state:
        logger.info(
            "Found a saved show state from a previous run — waiting for operator "
            "to resume (/api/resume-show) or discard (/api/discard-show) it."
        )

    # Run drone manager connections in background with error handling
    async def run_connections():
        try:
            await drone_manager.connect_all()
        except Exception as e:
            logger.error(f"Failed to initialize drone connections on GCS startup: {e}")
    asyncio.create_task(run_connections())

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Ground Control Station...")
    await drone_manager.shutdown()
    # Processes the GCS spawned are the GCS's to clean up. Without this an API
    # restart orphans a whole PX4/Gazebo fleet, and the next launch fails on a
    # port and Gazebo-partition collision.
    if sim_manager.processes:
        logger.info("Stopping simulation processes started by this GCS...")
        await sim_manager.stop()

@app.get("/api/status")
async def get_status():
    """Point-in-time fleet health, for `tara status` and any scripted check.

    Deliberately cheap and synchronous: it reads the telemetry dict the
    monitor loops already maintain rather than polling the vehicles, so a
    caller can hit it on a tight loop without competing with the 10Hz
    control/safety loops for the drones' attention.
    """
    telemetry = drone_manager.telemetry
    return {
        "backend": drone_manager.backend,
        "num_drones": len(drone_manager.ports),
        "connected": sum(1 for t in telemetry.values() if t.get("connected")),
        "armed": sum(1 for t in telemetry.values() if t.get("armed")),
        "trajectory_running": drone_manager.is_running_trajectory,
    }

@app.get("/api/preflight-check")
async def preflight_check():
    """Per-drone, per-rule pre-arm gate status under the active safety profile.

    Reads the telemetry dict the monitor loops maintain, like /api/status, so
    the dashboard's checklist HUD can poll it.
    """
    return preflight_engine.evaluate_manager(drone_manager)


@app.get("/api/preflight/audit")
async def preflight_audit(limit: int = 50):
    """Most recent audit-trail entries (latch/override steps, launch decisions)."""
    entries = list(preflight_engine.audit.recent)[-max(0, limit):]
    return {"entries": entries}


class ConfirmationRequest(BaseModel):
    operator: str = "operator"


class OverrideRequest(BaseModel):
    rules: list[str]
    reason: str
    operator: str = "operator"


class ConfirmationCommit(BaseModel):
    token: str
    operator: str = "operator"


@app.post("/api/preflight/latch/request")
async def preflight_latch_request(req: ConfirmationRequest = ConfirmationRequest()):
    """Step 1 of the two-step launch confirmation latch. Returns the token step 2 needs."""
    return preflight_engine.request_latch(operator=req.operator)


@app.post("/api/preflight/latch/confirm")
async def preflight_latch_confirm(req: ConfirmationCommit):
    """Step 2: confirms the latch. Consumed by the next accepted launch."""
    gates = preflight_engine.evaluate_manager(drone_manager)
    summary = {"launch_permitted": gates["launch_permitted"], "blocking_reasons": gates["blocking_reasons"]}
    try:
        return preflight_engine.confirm_latch(req.token, operator=req.operator, gates=summary)
    except ConfirmationError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/preflight/override/request")
async def preflight_override_request(req: OverrideRequest):
    """Step 1 of overriding BLOCKING rules the active profile marks overridable."""
    try:
        return preflight_engine.request_override(req.rules, req.reason, operator=req.operator)
    except ConfirmationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/preflight/override/confirm")
async def preflight_override_confirm(req: ConfirmationCommit):
    """Step 2: activates the override. Consumed by the next accepted launch."""
    try:
        return preflight_engine.confirm_override(req.token, operator=req.operator)
    except ConfirmationError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/launch")
async def launch_fleet():
    logger.info("API: Launching fleet...")
    decision = preflight_engine.authorize_launch(drone_manager)
    if not decision.permitted:
        # 412: the request is well-formed, but a precondition the server
        # enforces (a red pre-arm gate) does not hold.
        raise HTTPException(
            status_code=412,
            detail={
                "message": "Launch refused: pre-arm gates are red.",
                "profile": decision.report["profile"],
                "blocking_reasons": decision.report["blocking_reasons"],
            },
        )

    # Checked here as well as inside launch_sequence() so a refusal reaches the
    # operator as a 409, rather than only as a log line from the background task.
    if drone_manager.active_trajectory is not None or drone_manager.staged_show is not None:
        gate = drone_manager.launch_gate()
        if not gate["ok"]:
            raise HTTPException(status_code=409, detail=gate["reason"])

    # Wrap launch sequence task with error handling
    async def run_launch():
        try:
            await drone_manager.launch_sequence(
                drone_ids=decision.drone_ids,
                require_ekf_health="ekf_health" not in decision.overridden_rules,
            )
        except LaunchGateError as e:
            logger.error(f"Launch refused: {e}")
        except Exception as e:
            logger.error(f"Error during launch sequence execution: {e}")
    asyncio.create_task(run_launch())
    return {
        "status": "success",
        "message": "Launch sequence initiated.",
        "profile": decision.report["profile"],
        "drone_ids": decision.drone_ids,
        "excluded_drone_ids": decision.report["excluded_drone_ids"],
        "overridden_rules": sorted(decision.overridden_rules),
    }

@app.post("/api/rtl")
async def trigger_rtl():
    logger.info("API: Failsafe RTL triggered...")
    await drone_manager.trigger_rtl()
    return {"status": "success", "message": "Emergency RTL commanded."}

@app.post("/api/land")
async def trigger_land():
    logger.info("API: Land triggered...")
    await drone_manager.trigger_land()
    return {"status": "success", "message": "Emergency landing commanded."}

class StartTrajectoryRequest(BaseModel):
    mode: str = "LOCAL"
    trigger_epoch_ms: float = 0.0
    # "onboard" (drones fly their uploaded missions; GCS broadcasts the start)
    # or "streamed" (legacy 10Hz unicast setpoints, stock PX4 SITL only).
    execution: str = "onboard"

@app.post("/api/upload-trajectory")
async def upload_trajectory(file: UploadFile = File(...), signature: UploadFile | None = File(None)):
    """Accept a trajectory only if its detached .sig.json manifest (the
    optional ``signature`` part) verifies against a trusted key, or if the GCS
    runs with --allow-unsigned and no manifest was sent. A failed check is a
    403 and never touches the active trajectory."""
    logger.info(f"API: Uploading trajectory file: {file.filename}")
    
    # Check file size limit (5 MB max) to prevent OOM
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
    
    content_length = file.headers.get("content-length")
    if content_length and int(content_length) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 5MB.")
        
    content = bytearray()
    while True:
        chunk = await file.read(8192)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File too large. Maximum size is 5MB.")
            
    manifest = None
    if signature is not None:
        manifest = await signature.read(MAX_MANIFEST_BYTES + 1)
    try:
        verified = trust_policy.check(bytes(content), file.filename or "", manifest)
    except SignatureError as e:
        logger.warning(f"Rejected trajectory {file.filename!r}: {e}")
        raise HTTPException(status_code=403, detail=f"Show file rejected: {e}")
    if verified:
        logger.info(f"Trajectory {file.filename!r} signed by {verified.signer} (key {verified.key_id}).")

    try:
        parsed_trajectory = parse_trajectory_bytes(bytes(content), file.filename)

        # Save active trajectory
        drone_manager.active_trajectory = parsed_trajectory

        # Compute this show's two-tier geofence radii now (issue #53) so both
        # the live RTL monitor and the web UI (which reads it from this
        # response, not by recomputing it itself) have it before the operator
        # ever hits start-trajectory. start_trajectory() recomputes this again
        # from whatever trajectory actually starts playing, so this call just
        # makes it available immediately for display/verification.
        drone_manager._apply_geofence_envelope(parsed_trajectory)

        # Calculate summary details for response
        summary = {}
        for drone_id, wps in parsed_trajectory.items():
            summary[str(drone_id)] = {
                "waypoints_count": len(wps),
                "duration_seconds": wps[-1]["time"] if wps else 0.0
            }

        return {
            "status": "success",
            "message": "Trajectory uploaded and saved to active slot.",
            "drones_summary": summary,
            "trajectory_data": parsed_trajectory,
            "signature": (
                {"verified": True, "signer": verified.signer, "key_id": verified.key_id, "signed_at": verified.signed_at}
                if verified else {"verified": False}
            ),
            "geofence": {
                "soft_warning_radius_m": drone_manager.soft_geofence_radius,
                "hard_geofence_radius_m": drone_manager.geofence_radius,
            },
        }
    except Exception as e:
        logger.error(f"Failed parsing trajectory file: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/upload-mission")
async def upload_mission():
    """Compile the loaded trajectory into one mission per drone, upload each
    over MAVLink FTP, and verify every copy's CRC-32 (issue #47). Arming is
    refused until this has succeeded for the whole fleet."""
    logger.info("API: Uploading show missions to the fleet...")
    if not drone_manager.active_trajectory:
        raise HTTPException(status_code=400, detail="No active trajectory loaded. Please upload a trajectory first.")
    try:
        return await drone_manager.upload_show(drone_manager.active_trajectory)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/mission-status")
async def mission_status():
    """Per-drone mission upload/verification state and whether arming is permitted."""
    return drone_manager.mission_status()

@app.post("/api/verify-launch-geometry")
async def verify_launch_geometry(tolerance_m: float = 0.5):
    logger.info("API: Verifying launch geometry...")
    if not drone_manager.active_trajectory:
         raise HTTPException(status_code=400, detail="No active trajectory loaded. Please upload a trajectory first.")
    
    report = drone_manager.verify_launch_geometry(drone_manager.active_trajectory, tolerance_m)
    return report

@app.post("/api/start-trajectory")
async def start_trajectory(req: StartTrajectoryRequest):
    logger.info(
        f"API: Starting trajectory in {req.mode} mode, {req.execution} execution, "
        f"trigger_epoch_ms={req.trigger_epoch_ms}..."
    )
    if not drone_manager.active_trajectory:
         raise HTTPException(status_code=400, detail="No active trajectory loaded. Please upload a trajectory first.")
         
    try:
        drone_manager.start_trajectory(
            drone_manager.active_trajectory, 
            mode=req.mode, 
            trigger_epoch_ms=req.trigger_epoch_ms,
            execution=req.execution,
        )
        return {"status": "success", "message": f"Trajectory started in {req.mode} mode ({req.execution} execution)."}
    except Exception as e:
        logger.error(f"Failed starting trajectory: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/stop-trajectory")
async def stop_trajectory():
    logger.info("API: Stopping trajectory playback...")
    drone_manager.stop_trajectory()
    return {"status": "success", "message": "Trajectory playback stopped."}

@app.get("/api/saved-show-state")
async def get_saved_show_state():
    """
    Reports whether a saved show from a previous run (crash or restart
    mid-show, issue #4) is pending resume/discard, and a summary of it for
    the operator to decide with.
    """
    state = drone_manager.pending_resume_state
    if not state:
        return {"pending": False}

    elapsed = max(0.0, state["saved_at"] - state["show_started_at"])
    return {
        "pending": True,
        "mode": state["mode"],
        "elapsed_seconds": round(elapsed, 1),
        "saved_at": state["saved_at"],
        "drones_summary": {
            str(drone_id): {"waypoints_count": len(wps)}
            for drone_id, wps in state["trajectory"].items()
        },
    }

@app.post("/api/resume-show")
async def resume_show():
    """
    Reloads a saved show's trajectory and playback position so the operator
    can continue it. Deliberately does NOT re-arm or re-launch drones — the
    operator still has to trigger /api/launch and /api/start-trajectory
    themselves once they're ready.
    """
    logger.info("API: Resuming saved show state...")
    try:
        elapsed = drone_manager.resume_pending_show()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "status": "success",
        "message": "Saved show state reloaded. Launch and start the trajectory explicitly to continue.",
        "mode": drone_manager.show_mode,
        "elapsed_seconds": round(elapsed, 1),
        "trajectory_data": drone_manager.active_trajectory,
    }

@app.post("/api/discard-show")
async def discard_show():
    """Discards a saved show from a previous run without resuming it."""
    logger.info("API: Discarding saved show state...")
    drone_manager.discard_pending_show()
    return {"status": "success", "message": "Saved show state discarded."}

@app.get("/api/export-telemetry")
async def export_telemetry(filename: str = "telemetry_export.csv"):
    """Exports per-drone telemetry recorded during the last show as a downloadable CSV."""
    logger.info(f"API: Exporting recorded telemetry as {filename}...")
    if not drone_manager.telemetry_log:
        raise HTTPException(status_code=400, detail="No telemetry has been recorded yet. Run a trajectory first.")

    # Sanitize to a bare filename to avoid writing outside the exports directory.
    safe_filename = os.path.basename(filename) or "telemetry_export.csv"
    export_dir = os.path.join(os.path.dirname(__file__), "exports")
    os.makedirs(export_dir, exist_ok=True)
    filepath = os.path.join(export_dir, safe_filename)

    row_count = drone_manager.export_telemetry_csv(filepath)
    logger.info(f"API: Wrote {row_count} telemetry rows to {filepath}.")
    return FileResponse(filepath, media_type="text/csv", filename=safe_filename)

# --- Simulation process orchestration (issue #2) -------------------------
#
# These endpoints let the dashboard start and stop the simulation stack itself,
# so running a show does not require a terminal and `tara up`. The `tara` CLI
# remains the developer path and is unchanged; this is the operator path.

class SimLaunchRequest(BaseModel):
    """Body for POST /api/sim/launch.

    Attributes:
        num_drones: Fleet size to spawn.
        backend: "px4" for real SITL + Gazebo, "sim" for the vectorized fleet.
        headless: Run Gazebo without its GUI. Ignored for the sim backend.
    """
    num_drones: int = 5
    backend: str = "px4"
    headless: bool = True


class RvizLaunchRequest(BaseModel):
    world: str = "drone_show_field"


@app.post("/api/sim/launch")
async def sim_launch(req: SimLaunchRequest):
    """Spawn the Gazebo/PX4 SITL fleet (or register the in-process sim fleet)."""
    logger.info(
        f"API: Launching simulation — backend={req.backend}, drones={req.num_drones}, "
        f"headless={req.headless}"
    )
    try:
        status = await sim_manager.launch(
            num_drones=req.num_drones, backend=req.backend, headless=req.headless
        )
    except SimLaunchError as e:
        # 409 for "already running", 501 for an unsupported platform, 400 for a
        # bad request — an operator clicking Launch twice should not see the
        # same error shape as one running the dashboard on Windows.
        if "already running" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        if "POSIX-only" in str(e):
            raise HTTPException(status_code=501, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "message": "Simulation launch initiated.", "sim": status}


@app.post("/api/sim/stop")
async def sim_stop():
    """Terminate every simulation process, escalating SIGINT -> SIGTERM -> SIGKILL."""
    logger.info("API: Stopping simulation...")
    result = await sim_manager.stop()
    return {
        "status": "success",
        "message": f"Stopped {len(result['stopped'])} process(es).",
        "stopped": result["stopped"],
        "sim": result["status"],
    }


@app.get("/api/sim/status")
async def sim_status():
    """Report simulation PIDs, CPU/RAM, MAVLink ports, and fleet heartbeat.

    Merges the process view (from SimManager) with the fleet view (from the
    telemetry dict the monitor loops maintain), because the dashboard's status
    pills need both and asking it to correlate two endpoints would just move
    that join into the browser.
    """
    status = sim_manager.status()
    telemetry = drone_manager.telemetry
    connected = sum(1 for t in telemetry.values() if t.get("connected"))
    status["heartbeat"] = {
        # A fleet is only "alive" if something is actually reporting in —
        # processes running with zero connected drones is precisely the
        # half-started state an operator needs to see before pressing Launch.
        "alive": connected > 0,
        "connected": connected,
        "armed": sum(1 for t in telemetry.values() if t.get("armed")),
        "expected": len(drone_manager.ports),
        "home_locked": sum(
            1 for t in telemetry.values()
            if t.get("home_lat") not in (None, 0.0) or t.get("home_lon") not in (None, 0.0)
        ),
    }
    return status


@app.post("/api/rviz/launch")
async def rviz_launch(req: RvizLaunchRequest = RvizLaunchRequest()):
    """Launch RViz2 with the pre-configured drone-swarm display."""
    logger.info(f"API: Launching RViz on world '{req.world}'...")
    try:
        status = await sim_manager.launch_rviz(world=req.world)
    except SimLaunchError as e:
        if "already running" in str(e):
            raise HTTPException(status_code=409, detail=str(e))
        if "POSIX-only" in str(e):
            raise HTTPException(status_code=501, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "success", "message": "RViz launch initiated.", "sim": status}


@app.websocket("/ws/telemetry")
async def websocket_telemetry_endpoint(websocket: WebSocket):
    """Streams two channels on one socket instead of the full telemetry dict as
    JSON every tick (which serializes several hundred KB/s at 100 drones and
    stops being viable at 1000). See tara_gcs.telemetry_codec for the format.

    Binary (ArrayBuffer) frames at FAST_HZ carry position/heading/armed/
    connected — what a live view needs to animate. JSON text frames at
    SLOW_HZ carry battery/GPS/flight-mode/latency/home. The client tells them
    apart by `event.data`'s type; no wrapper protocol needed.
    """
    await websocket.accept()
    logger.info("WebSocket telemetry client connected.")
    fast_interval = 1.0 / FAST_HZ
    slow_interval = 1.0 / SLOW_HZ
    next_slow_send = 0.0
    elapsed = 0.0
    try:
        while True:
            await websocket.send_bytes(encode_fast_frame(drone_manager.telemetry))
            if elapsed >= next_slow_send:
                await websocket.send_json(encode_slow_frame(drone_manager.telemetry))
                next_slow_send = elapsed + slow_interval
            await asyncio.sleep(fast_interval)
            elapsed += fast_interval
    except WebSocketDisconnect:
        logger.info("WebSocket telemetry client disconnected.")
    except Exception as e:
        logger.error(f"WebSocket telemetry error: {e}")

if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("GCS_HOST", "0.0.0.0")
    port = int(os.environ.get("GCS_PORT", 8090))
    uvicorn.run("tara_gcs.main:app", host=host, port=port, reload=True)
