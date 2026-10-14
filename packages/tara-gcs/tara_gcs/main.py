import asyncio
from enum import Enum
import logging
import os
from pathlib import Path
import time
from typing import Annotated
from fastapi import (
    Depends, FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect,
    WebSocketException, status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from tara_gcs.drone_manager import DroneManager
from tara_gcs.fleet_fsm import Command, IllegalTransitionError
from tara_gcs.trajectory_parser import parse_trajectory_bytes
from tara_gcs.telemetry_codec import FAST_HZ, SLOW_HZ, encode_fast_frame, encode_slow_frame
from tara_gcs.sim_manager import SimLaunchError, SimManager
from tara_gcs.show_auth import load_trust_policy
from tara_gcs.auth import (
    AuthError, AuthService, LoginThrottled, Operator, OperatorStore, Role, SessionStore, load_auth_config,
)
from tara_core.signing import MAX_MANIFEST_BYTES, SignatureError
from tara_gcs.preflight import (
    DEFAULT_AUDIT_PATH, AuditLog, ConfirmationError, PreArmChecklistEngine, load_safety_config,
)

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

# --- Operating Mode & Hardware Boundary (issue #131) ------------------------
# Explicit separation between SIMULATION and HARDWARE modes. Commands are never
# silently retargeted, and physical flight requires separate hardware sign-off (#135).

class OperatingMode(str, Enum):
    SIMULATION = "SIMULATION"
    HARDWARE = "HARDWARE"

current_operating_mode: str = os.environ.get("GCS_OPERATING_MODE", "SIMULATION").upper()
hardware_flight_certified: bool = False

HARDWARE_PROFILE = {
    "autopilot": "Pixhawk FMUv2 (ArduPilot Quad-X)",
    "firmware": "ArduCopter 3.6.8 / 4.x",
    "serial_interface": os.environ.get("HARDWARE_SERIAL_PORT", "/dev/ttyACM0"),
    "baud_rate": int(os.environ.get("HARDWARE_BAUD_RATE", 115200)),
    "telemetry_radio": os.environ.get("HARDWARE_RADIO_PORT", "/dev/ttyUSB0"),
    "telemetry_baud": int(os.environ.get("HARDWARE_RADIO_BAUD", 57600)),
    "rc_receiver": "FlySky FS-iA6B (PPM, 8ch)",
    "rc_switch_channel": 5,
    "rc_modes": {
        "1000us": "Stabilize",
        "1500us": "AltHold",
        "2000us": "Land",
    },
    "battery_config": "3S LiPo, BATT_VOLT_MULT=10.938",
    "failsafes": "FS_THR_ENABLE=1, FS_EKF_ACTION=1, BATT_FS_LOW_ACT=1",
    "bench_audit": "diagnostics/pre_prop_audit/REPORT.md",
}

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
if os.environ.get("GCS_AUDIT_PATH"):
    kwargs["audit_path"] = os.environ["GCS_AUDIT_PATH"]
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

# --- Fleet lifecycle gating (issue #52) -----------------------------------
#
# Every command that changes what the fleet is doing is checked against the
# lifecycle state machine first; an illegal one is a 409 carrying the machine's
# own explanation, and nothing is sent to a vehicle.

# --- Operator authentication and roles (issue #64) ------------------------
#
# Every route except login and the bare health check needs a logged-in
# operator with at least the role its dependency names (see tara_gcs.auth).
# The operator recorded in the lifecycle and pre-arm audit logs is the one the
# session belongs to, not anything the client claims.

#: Who did what, from where (issue #64): logins, and every state-changing API
#: call with its outcome. Kept apart from the pre-arm trail so the checklist
#: HUD's recent entries are not crowded out by routine commands.
command_audit = AuditLog(
    path=os.environ.get("GCS_COMMAND_AUDIT_PATH")
    or os.path.join(os.path.dirname(DEFAULT_AUDIT_PATH), "command_audit.jsonl")
)

auth_config = load_auth_config(_tara_toml_path)
auth_service = AuthService(
    OperatorStore(auth_config.operators_path),
    SessionStore(ttl_s=auth_config.session_ttl_s),
)
if not auth_service.store.all():
    logger.warning(
        f"No GCS operators in {auth_config.operators_path}: nobody can log in until one is added with "
        "`python -m tara_gcs.auth add <name> --role flight_commander`."
    )

_bearer = HTTPBearer(auto_error=False)

#: Browsers cannot set headers on a WebSocket, so the dashboard offers its
#: token as a subprotocol (`new WebSocket(url, [WS_AUTH_PROTOCOL, token])`)
#: rather than in the URL, where it would end up in access logs.
WS_AUTH_PROTOCOL = "tara.bearer"


def _origin(request: Request | WebSocket) -> str:
    return request.client.host if request.client else "unknown"


def current_operator(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Operator:
    """The logged-in operator, or 401."""
    try:
        operator = auth_service.authenticate(creds.credentials if creds else None)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=str(e), headers={"WWW-Authenticate": "Bearer"})
    request.state.operator = operator
    return operator


def require(role: Role):
    """Dependency: the logged-in operator if they hold at least `role`, else 403."""
    def dependency(operator: Annotated[Operator, Depends(current_operator)]) -> Operator:
        if operator.role < role:
            raise HTTPException(
                status_code=403,
                detail=f"{operator.id} is a {operator.role.name}; this needs {role.name} or higher.",
            )
        return operator
    dependency.required_role = role  # read by the route-coverage test
    return dependency


Viewer = Annotated[Operator, Depends(require(Role.VIEWER))]
Choreographer = Annotated[Operator, Depends(require(Role.CHOREOGRAPHER))]
Commander = Annotated[Operator, Depends(require(Role.FLIGHT_COMMANDER))]


async def ws_operator(websocket: WebSocket) -> Operator:
    """WebSocket counterpart of require(Role.VIEWER); refuses with close code 1008."""
    offered = [p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",")]
    token = offered[offered.index(WS_AUTH_PROTOCOL) + 1] if WS_AUTH_PROTOCOL in offered[:-1] else None
    try:
        return auth_service.authenticate(token)
    except AuthError as e:
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason=str(e))


#: POSTs not written to the command audit trail by the middleware below:
#: login records its own outcome (without the passphrase).
_UNAUDITED_POSTS = {"/api/auth/login"}


@app.middleware("http")
async def audit_commands(request: Request, call_next):
    """Record every state-changing API call: who, from where, and the outcome.

    Refused calls (401/403/409/412) are recorded too: an attempt to launch
    without the right role is exactly what an audit trail is for.
    """
    response = await call_next(request)
    if request.method == "POST" and request.url.path.startswith("/api/") and request.url.path not in _UNAUDITED_POSTS:
        operator = getattr(request.state, "operator", None)
        command_audit.record(
            "command",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            operator=operator.id if operator else None,
            role=operator.role.name if operator else None,
            origin_ip=_origin(request),
        )
    return response


class LoginRequest(BaseModel):
    operator: str
    passphrase: str


@app.post("/api/auth/login")
def auth_login(req: LoginRequest, request: Request):
    """Exchange an operator's passphrase for a bearer session token.

    A plain `def`: scrypt verification takes tens of milliseconds of CPU, so
    FastAPI runs this in its threadpool rather than stalling the event loop the
    telemetry and safety loops share.
    """
    origin = _origin(request)
    try:
        token, session, operator = auth_service.login(req.operator, req.passphrase, origin)
    except LoginThrottled as e:
        command_audit.record("login_throttled", operator=req.operator, origin_ip=origin)
        raise HTTPException(status_code=429, detail=str(e), headers={"Retry-After": str(e.retry_after_s)})
    except AuthError as e:
        command_audit.record("login_failed", operator=req.operator, origin_ip=origin)
        raise HTTPException(status_code=401, detail=str(e), headers={"WWW-Authenticate": "Bearer"})
    command_audit.record("login", operator=operator.id, role=operator.role.name, origin_ip=origin)
    return {"token": token, "expires_at": session.expires_at, **operator.public()}


@app.get("/api/audit/commands")
async def command_audit_trail(limit: int = 100, *, operator: Viewer):
    """Most recent command-audit entries: logins and state-changing API calls."""
    return {"entries": list(command_audit.recent)[-max(0, limit):]}


@app.get("/api/auth/me")
async def auth_me(operator: Viewer):
    """Who the presented token belongs to, and their current role."""
    return operator.public()


@app.post("/api/auth/logout")
async def auth_logout(operator: Viewer, creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]):
    auth_service.sessions.revoke(creds.credentials)
    return {"status": "success", "message": f"{operator.id} logged out."}


def _check_command(command: Command, operator: str, trigger: str) -> None:
    try:
        drone_manager.fleet_fsm.check(command, operator=operator, trigger=trigger)
    except IllegalTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail())


def _apply_command(command: Command, operator: str, trigger: str) -> None:
    try:
        drone_manager.fleet_fsm.apply(command, operator=operator, trigger=trigger)
    except IllegalTransitionError as e:
        raise HTTPException(status_code=409, detail=e.detail())

@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Ground Control Station...")
    if current_operating_mode == "HARDWARE":
        if not hardware_flight_certified or drone_manager.backend != "hardware":
            logger.critical("Startup refused: GCS configured in HARDWARE mode without verified hardware backend and gate #135 certification.")
            raise RuntimeError(
                "GCS startup refused: configured operating mode HARDWARE requires a verified hardware controller transport and certified hardware acceptance gate #135."
            )

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
        "mode": current_operating_mode,
        "configured_mode": current_operating_mode,
        "backend": drone_manager.backend,
        "active_backend": drone_manager.backend,
        "hardware_flight_certified": hardware_flight_certified,
        "hardware_flight_available": (drone_manager.backend == "hardware" and hardware_flight_certified),
        "num_drones": len(drone_manager.ports),
        "connected": sum(1 for t in telemetry.values() if t.get("connected")),
        "armed": sum(1 for t in telemetry.values() if t.get("armed")),
        "trajectory_running": drone_manager.is_running_trajectory,
        "fleet_state": drone_manager.fleet_fsm.state.value,
        # Drones aborted out of the show on their own (issue #59); per-drone
        # detail is each drone's "aborted" telemetry field.
        "aborted": sorted(d_id for d_id, t in telemetry.items() if t.get("aborted")),
    }


class ModeSwitchRequest(BaseModel):
    mode: str
    confirmation: str


def check_flight_mode_and_hardware_gate(action_name: str):
    """
    Ensures hardware flight actions are completely unavailable and fail closed
    until a verified hardware backend exists and hardware gate #135 has passed.
    """
    if current_operating_mode == "HARDWARE":
        if not hardware_flight_certified or drone_manager.backend != "hardware":
            raise HTTPException(
                status_code=412,
                detail=f"Physical flight action '{action_name}' blocked: Hardware acceptance gate #135 has not passed and verified hardware controller transport is not configured.",
            )


@app.get("/api/mode")
async def get_mode(operator: Viewer):
    """Current operating mode (SIMULATION vs HARDWARE) and telemetry freshness."""
    now = time.time()
    drone0 = drone_manager.telemetry.get(0, {})
    last_hb = drone0.get("last_heartbeat")
    freshness_s = round(now - last_hb, 2) if last_hb else None
    is_fresh = freshness_s is not None and freshness_s < 2.5
    is_hardware_backend = drone_manager.backend == "hardware"
    hardware_available = is_hardware_backend and hardware_flight_certified
    return {
        "mode": current_operating_mode,
        "configured_mode": current_operating_mode,
        "backend": drone_manager.backend,
        "active_backend": drone_manager.backend,
        "hardware_profile": HARDWARE_PROFILE,
        "hardware_flight_certified": hardware_flight_certified,
        "hardware_flight_available": hardware_available,
        "telemetry_freshness_s": freshness_s,
        "telemetry_status": "FRESH" if is_fresh else "STALE",
        "rc_takeover_available": False,
        "rc_status": "UNVERIFIED",
    }


@app.post("/api/mode")
async def set_mode(req: ModeSwitchRequest, operator: Commander):
    """Switch operating mode. Requires explicit operator confirmation."""
    global current_operating_mode
    if operator.role < Role.FLIGHT_COMMANDER:
        raise HTTPException(
            status_code=403,
            detail=f"{operator.id} is a {operator.role.name}; this needs FLIGHT_COMMANDER.",
        )
    target_mode = req.mode.upper()
    if target_mode not in ("SIMULATION", "HARDWARE"):
        raise HTTPException(status_code=400, detail="Mode must be SIMULATION or HARDWARE")
    expected_conf = f"CONFIRM_{target_mode}"
    if req.confirmation != expected_conf:
        raise HTTPException(
            status_code=400,
            detail=f"Confirmation mismatch. Must supply confirmation='{expected_conf}'",
        )
    if target_mode == "HARDWARE":
        if not hardware_flight_certified or drone_manager.backend != "hardware":
            raise HTTPException(
                status_code=412,
                detail="Cannot switch to HARDWARE mode: verified physical flight controller transport is not configured and hardware acceptance gate #135 has not passed.",
            )
    current_operating_mode = target_mode
    command_audit.record(
        "mode_switch",
        target_mode=target_mode,
        operator=operator.id,
        role=operator.role.name,
    )
    return {
        "status": "success",
        "mode": current_operating_mode,
        "configured_mode": current_operating_mode,
        "active_backend": drone_manager.backend,
        "message": f"Operating mode switched to {current_operating_mode}",
    }


class SingleDroneSequenceRequest(BaseModel):
    target_altitude_m: float = Field(default=1.0, ge=0.3, le=5.0, description="Target altitude in meters (0.3m - 5.0m)")
    hover_duration_s: float = Field(default=5.0, ge=1.0, le=30.0, description="Hover duration in seconds (1.0s - 30.0s)")


@app.post("/api/single-drone/hover-sequence")
async def run_single_drone_sequence(
    req: SingleDroneSequenceRequest = SingleDroneSequenceRequest(),
    *,
    operator: Commander,
):
    """
    Supervised low-altitude 1m hover and slow landing demo progression.
    Requires SIMULATION mode or certified hardware acceptance gate (#135).
    """
    if operator.role < Role.FLIGHT_COMMANDER:
        raise HTTPException(
            status_code=403,
            detail=f"{operator.id} is a {operator.role.name}; this needs FLIGHT_COMMANDER.",
        )
    check_flight_mode_and_hardware_gate("hover-sequence")
    if 0 not in drone_manager.drones or not drone_manager.telemetry.get(0, {}).get("connected"):
        raise HTTPException(
            status_code=400,
            detail="Drone 0 is not connected.",
        )
    try:
        steps_log = await drone_manager.execute_single_drone_demo_sequence(
            drone_id=0,
            target_alt_m=req.target_altitude_m,
            hover_s=req.hover_duration_s,
            operator=operator.id,
        )
    except Exception as e:
        logger.error(f"Single drone sequence failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "status": "COMPLETED",
        "mode": current_operating_mode,
        "configured_mode": current_operating_mode,
        "active_backend": drone_manager.backend,
        "vehicle": "SIMULATED" if current_operating_mode == "SIMULATION" else "HARDWARE",
        "steps": steps_log,
    }

@app.get("/api/preflight-check")
async def preflight_check(operator: Viewer):
    """Per-drone, per-rule pre-arm gate status under the active safety profile.

    Reads the telemetry dict the monitor loops maintain, like /api/status, so
    the dashboard's checklist HUD can poll it.
    """
    return preflight_engine.evaluate_manager(drone_manager)


@app.get("/api/preflight/audit")
async def preflight_audit(limit: int = 50, *, operator: Viewer):
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
async def preflight_latch_request(req: ConfirmationRequest = ConfirmationRequest(), *, operator: Commander):
    """Step 1 of the two-step launch confirmation latch. Returns the token step 2 needs."""
    return preflight_engine.request_latch(operator=operator.id)


@app.post("/api/preflight/latch/confirm")
async def preflight_latch_confirm(req: ConfirmationCommit, operator: Commander):
    """Step 2: confirms the latch. Consumed by the next accepted launch."""
    gates = preflight_engine.evaluate_manager(drone_manager)
    summary = {"launch_permitted": gates["launch_permitted"], "blocking_reasons": gates["blocking_reasons"]}
    try:
        return preflight_engine.confirm_latch(req.token, operator=operator.id, gates=summary)
    except ConfirmationError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/preflight/override/request")
async def preflight_override_request(req: OverrideRequest, operator: Commander):
    """Step 1 of overriding BLOCKING rules the active profile marks overridable."""
    try:
        return preflight_engine.request_override(req.rules, req.reason, operator=operator.id)
    except ConfirmationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/preflight/override/confirm")
async def preflight_override_confirm(req: ConfirmationCommit, operator: Commander):
    """Step 2: activates the override. Consumed by the next accepted launch."""
    try:
        return preflight_engine.confirm_override(req.token, operator=operator.id)
    except ConfirmationError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.get("/api/fleet-state")
async def get_fleet_state(operator: Viewer):
    """The fleet's lifecycle state, which commands are legal in it, and the
    most recent transitions (the audit log on disk has the full record)."""
    fsm = drone_manager.fleet_fsm
    return {**fsm.snapshot(), "history": list(fsm.history)}

@app.post("/api/launch")
async def launch_fleet(operator: Commander):
    logger.info("API: Launching fleet...")
    check_flight_mode_and_hardware_gate("launch")
    _check_command(Command.LAUNCH, operator.id, "POST /api/launch")
    decision = preflight_engine.authorize_launch(drone_manager, operator=operator.id)
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

    _apply_command(Command.LAUNCH, operator.id, "POST /api/launch")
    drone_manager.start_launch_sequence(
        drone_ids=decision.drone_ids,
        require_ekf_health="ekf_health" not in decision.overridden_rules,
    )
    return {
        "status": "success",
        "message": "Launch sequence initiated.",
        "profile": decision.report["profile"],
        "drone_ids": decision.drone_ids,
        "excluded_drone_ids": decision.report["excluded_drone_ids"],
        "overridden_rules": sorted(decision.overridden_rules),
    }

@app.post("/api/rtl")
async def trigger_rtl(operator: Commander):
    logger.info("API: Failsafe RTL triggered...")
    if current_operating_mode == "HARDWARE" and drone_manager.backend != "hardware":
        raise HTTPException(
            status_code=503,
            detail="Physical emergency RTL unavailable: no verified physical hardware controller transport exists.",
        )
    await drone_manager.trigger_rtl(operator=operator.id, trigger="POST /api/rtl")
    return {
        "status": "success",
        "message": f"Emergency RTL commanded to active backend ({drone_manager.backend}).",
        "backend": drone_manager.backend,
        "mode": current_operating_mode,
    }

@app.post("/api/land")
async def trigger_land(operator: Commander):
    logger.info("API: Land triggered...")
    if current_operating_mode == "HARDWARE" and drone_manager.backend != "hardware":
        raise HTTPException(
            status_code=503,
            detail="Physical emergency landing unavailable: no verified physical hardware controller transport exists.",
        )
    await drone_manager.trigger_land(operator=operator.id, trigger="POST /api/land")
    return {
        "status": "success",
        "message": f"Emergency landing commanded to active backend ({drone_manager.backend}).",
        "backend": drone_manager.backend,
        "mode": current_operating_mode,
    }

@app.post("/api/estop")
async def trigger_estop(operator: Commander):
    """Kill every drone's motors. Airborne drones fall — see DroneManager.trigger_estop."""
    logger.critical("API: EMERGENCY STOP triggered...")
    if current_operating_mode == "HARDWARE" and drone_manager.backend != "hardware":
        raise HTTPException(
            status_code=503,
            detail="Physical emergency stop unavailable: no verified physical hardware controller transport exists.",
        )
    await drone_manager.trigger_estop(operator=operator.id, trigger="POST /api/estop")
    return {
        "status": "success",
        "message": f"Emergency stop commanded: motors killed on active backend ({drone_manager.backend}).",
        "backend": drone_manager.backend,
        "mode": current_operating_mode,
    }

# --- Single-drone abort (issue #59) ---------------------------------------
#
# Take one drone out of the show while the rest keep flying. Each response
# carries the clearance check of the drone's modelled descent path against the
# other drones' planned positions; a conflict is a warning in the body, never a
# refusal. A vehicle that rejects the command is a 502, not a success.

async def _abort_drone(drone_id: int, mode: str, label: str) -> dict:
    if current_operating_mode == "HARDWARE" and drone_manager.backend != "hardware":
        raise HTTPException(
            status_code=503,
            detail=f"Physical emergency {label} unavailable: no verified physical hardware controller transport exists.",
        )
    try:
        result = await drone_manager.abort_drone(drone_id, mode)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Drone {drone_id} is not connected to this GCS.")
    if not result["commanded"]:
        raise HTTPException(
            status_code=502,
            detail={"message": f"Drone {drone_id} rejected {label}: {result['error']}", "backend": drone_manager.backend, **result},
        )
    return {
        "status": "success",
        "message": f"Drone {drone_id} {label} commanded on active backend ({drone_manager.backend}).",
        "backend": drone_manager.backend,
        "mode": current_operating_mode,
        **result,
    }

@app.post("/api/drone/{drone_id}/rtl")
async def abort_drone_rtl(drone_id: int, operator: Commander):
    logger.info(f"API: Single-drone RTL triggered for Drone {drone_id}...")
    return await _abort_drone(drone_id, "rtl", "RTL")

@app.post("/api/drone/{drone_id}/land")
async def abort_drone_land(drone_id: int, operator: Commander):
    logger.info(f"API: Single-drone land triggered for Drone {drone_id}...")
    return await _abort_drone(drone_id, "land", "landing")

@app.post("/api/drone/{drone_id}/estop")
async def abort_drone_estop(drone_id: int, operator: Commander):
    """Kill one drone's motors; it falls. See DroneManager._estop_one."""
    logger.critical(f"API: Single-drone EMERGENCY STOP triggered for Drone {drone_id}...")
    return await _abort_drone(drone_id, "estop", "emergency stop")

class StartTrajectoryRequest(BaseModel):
    mode: str = "LOCAL"
    trigger_epoch_ms: float = 0.0
    # "onboard" (drones fly their uploaded missions; GCS broadcasts the start)
    # or "streamed" (legacy 10Hz unicast setpoints, stock PX4 SITL only).
    execution: str = "onboard"

@app.post("/api/upload-trajectory")
async def upload_trajectory(
    file: UploadFile = File(...),
    signature: UploadFile | None = File(None),
    *,
    operator: Choreographer,
):
    """Accept a trajectory only if its detached .sig.json manifest (the
    optional ``signature`` part) verifies against a trusted key, or if the GCS
    runs with --allow-unsigned and no manifest was sent. A failed check is a
    403 and never touches the active trajectory."""
    logger.info(f"API: Uploading trajectory file: {file.filename}")
    _check_command(Command.UPLOAD_TRAJECTORY, operator.id, "POST /api/upload-trajectory")
    
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
async def upload_mission(operator: Commander):
    """Compile the loaded trajectory into one mission per drone, upload each
    over MAVLink FTP, and verify every copy's CRC-32 (issue #47). Arming is
    refused until this has succeeded for the whole fleet."""
    logger.info("API: Uploading show missions to the fleet...")
    _check_command(Command.UPLOAD_MISSION, operator.id, "POST /api/upload-mission")
    if not drone_manager.active_trajectory:
        raise HTTPException(status_code=400, detail="No active trajectory loaded. Please upload a trajectory first.")
    try:
        return await drone_manager.upload_show(drone_manager.active_trajectory)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/mission-status")
async def mission_status(operator: Viewer):
    """Per-drone mission upload/verification state and whether arming is permitted."""
    return drone_manager.mission_status()

@app.post("/api/verify-launch-geometry")
async def verify_launch_geometry(tolerance_m: float = 0.5, *, operator: Choreographer):
    logger.info("API: Verifying launch geometry...")
    if not drone_manager.active_trajectory:
         raise HTTPException(status_code=400, detail="No active trajectory loaded. Please upload a trajectory first.")
    
    report = drone_manager.verify_launch_geometry(drone_manager.active_trajectory, tolerance_m)
    return report

@app.post("/api/start-trajectory")
async def start_trajectory(req: StartTrajectoryRequest, operator: Commander):
    check_flight_mode_and_hardware_gate("start-trajectory")
    logger.info(
        f"API: Starting trajectory in {req.mode} mode, {req.execution} execution, "
        f"trigger_epoch_ms={req.trigger_epoch_ms}..."
    )
    trigger = "POST /api/start-trajectory"
    # Checked, then acted on, then committed: a start that fails with a 400
    # must leave the fleet in FORMATION_HOLD, not in a RUNNING with nothing
    # running. Nothing below awaits, so no other command can land in between.
    _check_command(Command.START_TRAJECTORY, operator.id, trigger)
    if not drone_manager.active_trajectory:
         raise HTTPException(status_code=400, detail="No active trajectory loaded. Please upload a trajectory first.")
         
    try:
        drone_manager.start_trajectory(
            drone_manager.active_trajectory, 
            mode=req.mode, 
            trigger_epoch_ms=req.trigger_epoch_ms,
            execution=req.execution,
        )
    except Exception as e:
        logger.error(f"Failed starting trajectory: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    _apply_command(Command.START_TRAJECTORY, operator.id, trigger)
    return {"status": "success", "message": f"Trajectory started in {req.mode} mode ({req.execution} execution)."}

@app.post("/api/stop-trajectory")
async def stop_trajectory(operator: Commander):
    logger.info("API: Stopping trajectory playback...")
    _check_command(Command.STOP_TRAJECTORY, operator.id, "POST /api/stop-trajectory")
    drone_manager.stop_trajectory()
    _apply_command(Command.STOP_TRAJECTORY, operator.id, "POST /api/stop-trajectory")
    return {"status": "success", "message": "Trajectory playback stopped."}

@app.get("/api/saved-show-state")
async def get_saved_show_state(operator: Viewer):
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
async def resume_show(operator: Commander):
    """
    Reloads a saved show's trajectory and playback position so the operator
    can continue it. Deliberately does NOT re-arm or re-launch drones — the
    operator still has to trigger /api/launch and /api/start-trajectory
    themselves once they're ready.
    """
    logger.info("API: Resuming saved show state...")
    check_flight_mode_and_hardware_gate("resume-show")
    _check_command(Command.RESUME_SHOW, operator.id, "POST /api/resume-show")
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
async def discard_show(operator: Commander):
    """Discards a saved show from a previous run without resuming it."""
    logger.info("API: Discarding saved show state...")
    _check_command(Command.DISCARD_SHOW, operator.id, "POST /api/discard-show")
    drone_manager.discard_pending_show()
    return {"status": "success", "message": "Saved show state discarded."}

@app.get("/api/export-telemetry")
async def export_telemetry(filename: str = "telemetry_export.csv", *, operator: Viewer):
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
async def sim_launch(req: SimLaunchRequest, operator: Commander):
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
async def sim_stop(operator: Commander):
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
async def sim_status(operator: Viewer):
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
async def rviz_launch(req: RvizLaunchRequest = RvizLaunchRequest(), *, operator: Commander):
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
async def websocket_telemetry_endpoint(websocket: WebSocket, operator: Annotated[Operator, Depends(ws_operator)]):
    """Streams two channels on one socket instead of the full telemetry dict as
    JSON every tick (which serializes several hundred KB/s at 100 drones and
    stops being viable at 1000). See tara_gcs.telemetry_codec for the format.

    Binary (ArrayBuffer) frames at FAST_HZ carry position/heading/armed/
    connected — what a live view needs to animate. JSON text frames at
    SLOW_HZ carry battery/GPS/flight-mode/latency/home. The client tells them
    apart by `event.data`'s type; no wrapper protocol needed.

    The slow frame also carries the fleet lifecycle state (issue #52). A
    transition sends one immediately rather than waiting out the 1Hz cadence,
    so the HUD's state and enabled buttons are never a second behind a
    command the operator just issued.
    """
    await websocket.accept(subprotocol=WS_AUTH_PROTOCOL)
    logger.info(f"WebSocket telemetry client connected ({operator.id}).")
    fast_interval = 1.0 / FAST_HZ
    slow_interval = 1.0 / SLOW_HZ
    next_slow_send = 0.0
    elapsed = 0.0
    sent_fsm_seq = None
    try:
        while True:
            await websocket.send_bytes(encode_fast_frame(drone_manager.telemetry))
            fsm = drone_manager.fleet_fsm
            if elapsed >= next_slow_send or fsm.seq != sent_fsm_seq:
                sent_fsm_seq = fsm.seq
                await websocket.send_json(encode_slow_frame(drone_manager.telemetry, fsm.snapshot()))
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
