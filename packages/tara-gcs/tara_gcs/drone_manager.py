import asyncio
import csv
import functools
import gc
import math
import logging
import os
import shutil
import tempfile
import time
import numpy as np
from scipy.spatial import cKDTree
from tara_core import (
    DEFAULT_LIMITS,
    MAX_FLEET_SIZE,
    ShowCommand,
    altitude_layers,
    compile_mission,
    compute_show_id,
    encode_show_trigger,
    interpolate_waypoint,
    launch_grid,
    mission_crc32,
    sweep_times,
)
from tara_core.show import Show
from mavsdk import System
from mavsdk.offboard import PositionNedYaw, PositionGlobalYaw, OffboardError, VelocityNedYaw, AccelerationNed
from tara_gcs.broadcast_manager import BROADCAST_SYSID
from tara_gcs.preflight import GNSS_FIX_TYPES
from tara_gcs.fleet_fsm import (
    AIRBORNE_STATES,
    DEFAULT_AUDIT_PATH,
    EMERGENCY_STATES,
    SYSTEM_OPERATOR,
    Command,
    FleetStateMachine,
    LifecycleState,
)
from tara_gcs.state_store import DEFAULT_STATE_PATH, discard_show_state, load_show_state, save_show_state
from tara_gcs.trajectory_parser import gate_trajectory

# Fleet-wide safety limits and launch geometry come from tara_core so that this
# runtime monitor, the upload gate and the Studio's design-time validator can no
# longer disagree. Previously the altitudes here rose in 0.7m steps — below both
# the 0.8m the Studio checked and the 1.5m this module aborts on, meaning a
# fleet was in violation while still hovering over its own pads.
#
# MAX_FLEET_SIZE now lives in tara_core (not a local constant here) so the
# Studio generator's default spawn positions are built from the exact same
# fixed-size grid — the two previously disagreed past 32 drones, since this
# module always used tara_core.launch_grid(MAX_FLEET_SIZE) while the Studio
# generator used its own 1D line, and the two only coincided by accident for
# fleets that fit in the grid's first row.
DEFAULT_ALTITUDES = altitude_layers(MAX_FLEET_SIZE)

# Each drone's PX4/SITL local NED origin is set at its own spawn/launch pad, so
# per-drone "local_x/y/z" telemetry is NOT directly comparable across drones.
# sim/px4/launch_fleet.sh spawns pads on the same grid tara_core.launch_grid
# defines, and the Studio's generator bakes that grid into trajectory
# coordinates (a shared "fleet frame"). This offset converts between the two.
LAUNCH_PAD_SPACING_Y_M = DEFAULT_LIMITS.pad_spacing_m

#: How long a drone gets to report both is_armable and is_home_position_ok
#: before launch_sequence gives up on it. Raised from the old armable-only 15s
#: because home lock legitimately takes longer than arming readiness: EKF2 has
#: to converge on the GNSS origin PX4_HOME_LAT/LON/ALT anchors it to, and under
#: the physics load of a multi-drone SITL fleet that convergence is measurably
#: slower than on a single instance. 15s was enough for is_armable alone and is
#: not enough for both.
HOME_LOCK_TIMEOUT_S = 45.0


#: Where each drone's compiled mission lives on its own storage. PX4 exposes the
#: SD card to MAVLink FTP under /fs/microsd.
MISSION_REMOTE_DIR = "/fs/microsd/tara"
MISSION_FILE_NAME = "show.tmsn"

#: How far ahead of "now" the broadcast start instant is set when the operator
#: asks to start immediately. Long enough to hand every drone off from offboard
#: and get the trigger repeated a few times before t=0, so a drone that misses
#: one frame on a lossy radio still hears another in time.
TRIGGER_LEAD_S = 1.0
TRIGGER_REPEAT_INTERVAL_S = 0.2


class LaunchGateError(RuntimeError):
    """Arming refused because the fleet does not hold a verified copy of the show."""


class OperatorTakeoverError(RuntimeError):
    """A pilot moved the vehicle out of OFFBOARD mid-sequence (issue #171).

    The single-drone sequence yields without issuing any command of its own:
    the pilot, not this sequence, is flying the vehicle now.
    """


#: Pads for the largest fleet we support, computed once. Indexing into a single
#: fixed grid matters: deriving it per-drone from the current fleet size would
#: move a drone's pad every time the fleet grew, because the grid's width is
#: ceil(sqrt(n)).
_PAD_GRID = launch_grid(MAX_FLEET_SIZE)
_PAD_ARRAY = np.asarray(_PAD_GRID)  # the same grid, for vectorized fleet-frame shifts


#: Bulk telemetry cadence on backend="sim" (issue #51): the fast fields
#: (connection, armed, position) every tick, the slow ones (battery, flight
#: mode, GPS, home) every SIM_SLOW_TELEMETRY_EVERY ticks. 20Hz and 2Hz, the
#: same rates the per-drone SimSystem streams this replaces were read at.
SIM_TELEMETRY_INTERVAL_S = 0.05
SIM_SLOW_TELEMETRY_EVERY = 10


def _hover_altitude(drone_id: int) -> float:
    """Altitude, metres above the pad, launch_sequence climbs this drone to."""
    return DEFAULT_ALTITUDES[drone_id] if drone_id < len(DEFAULT_ALTITUDES) else 3.0


def _pad_offset(drone_id: int) -> tuple[float, float]:
    """Returns (x, y) offset of a drone's own launch pad within the shared fleet frame."""
    if not 0 <= drone_id < MAX_FLEET_SIZE:
        raise ValueError(
            f"drone_id {drone_id} outside supported fleet size 0..{MAX_FLEET_SIZE - 1}"
        )
    return _PAD_GRID[drone_id]

# --- Single-drone abort (issue #59) ---
#
# Aborting one drone (RTL, LAND or ESTOP) takes it out of the show while the
# rest of the fleet keeps flying. Before the drone descends through a live
# formation, the GCS checks the path it is *expected* to take against every
# other drone's planned positions. "Expected" is the honest word: after the
# command the flight controller owns the trajectory, so the check runs against
# a deliberately conservative model and can only ever warn, never block.
#
# The model, per mode (fleet frame, altitude positive up):
#   * RTL   — horizontal leg from the current position to the drone's own pad
#             at its current altitude, then straight down. This is what the sim
#             does, and what PX4 does only if RTL_RETURN_ALT / RTL_CONE_ANG are
#             configured not to climb first; PX4's default RTL climbs to 60m,
#             which this model does not predict.
#   * LAND  — straight down from the current position.
#   * ESTOP — straight down, falling between half and full gravity. Ballistic
#             drift from the drone's velocity at the moment of the kill is not
#             modelled.
# Timing is uncertain, so each mode has a slowest and fastest speed. At any
# instant the drone is somewhere on the stretch of path between the two, and a
# neighbour is checked against that whole stretch, not a single point.

ABORT_MODES = ("rtl", "land", "estop")

#: RTL horizontal leg speed range, m/s. PX4 flies it at MPC_XY_CRUISE (5 m/s by
#: default); the sim's tracker can reach its 8 m/s velocity cap.
ABORT_RTL_SPEED_RANGE_MS = (1.0, 8.0)
#: LAND / RTL descent rate range, m/s. PX4's MPC_LAND_SPEED defaults to 0.7 and
#: its faster high-altitude descent to 1.0-1.5; the sim descends at 1.0.
ABORT_DESCENT_RATE_RANGE_MS = (0.5, 1.5)
#: ESTOP fall acceleration range, m/s^2: drag-limited to free fall.
ABORT_FALL_ACCEL_RANGE_MS2 = (0.5 * 9.81, 9.81)
#: Sample interval for the clearance sweep, seconds.
ABORT_CLEARANCE_DT_S = 0.1

_ABORT_MODEL_DESCRIPTIONS = {
    "rtl": (
        "horizontal leg to own pad at current altitude "
        f"({ABORT_RTL_SPEED_RANGE_MS[0]}-{ABORT_RTL_SPEED_RANGE_MS[1]} m/s), then vertical descent "
        f"({ABORT_DESCENT_RATE_RANGE_MS[0]}-{ABORT_DESCENT_RATE_RANGE_MS[1]} m/s); "
        "does not model PX4's default RTL climb"
    ),
    "land": (
        "vertical descent from current position "
        f"({ABORT_DESCENT_RATE_RANGE_MS[0]}-{ABORT_DESCENT_RATE_RANGE_MS[1]} m/s)"
    ),
    "estop": "vertical fall from current position (0.5g-1g); ballistic drift not modelled",
}


def _abort_horizon(mode: str, horizontal_m: float, vertical_m: float) -> float:
    """Seconds until even the slowest modelled timing has reached the ground."""
    if mode == "estop":
        return math.sqrt(2.0 * vertical_m / ABORT_FALL_ACCEL_RANGE_MS2[0])
    return horizontal_m / ABORT_RTL_SPEED_RANGE_MS[0] + vertical_m / ABORT_DESCENT_RATE_RANGE_MS[0]


def _abort_progress(mode: str, horizontal_m: float, vertical_m: float, tau: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Distance travelled along the abort path at each time ``tau``, slowest and fastest.

    The path is two legs, horizontal then vertical (``horizontal_m`` is zero
    for LAND/ESTOP), measured as one arc length from the start.
    """
    total = horizontal_m + vertical_m
    if mode == "estop":
        a_slow, a_fast = ABORT_FALL_ACCEL_RANGE_MS2
        return np.minimum(0.5 * a_slow * tau**2, total), np.minimum(0.5 * a_fast * tau**2, total)

    def along(h_speed: float, v_rate: float) -> np.ndarray:
        leg1_t = horizontal_m / h_speed
        return np.minimum(
            np.where(tau <= leg1_t, h_speed * tau, horizontal_m + v_rate * (tau - leg1_t)),
            total,
        )

    (h_slow, h_fast), (v_slow, v_fast) = ABORT_RTL_SPEED_RANGE_MS, ABORT_DESCENT_RATE_RANGE_MS
    return along(h_slow, v_slow), along(h_fast, v_fast)


def _distance_to_leg(q: np.ndarray, origin: np.ndarray, unit: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Distance from points ``q`` (S, 3) to the stretch ``[lo, hi]`` of a line, per sample."""
    t = np.clip((q - origin) @ unit, lo, hi)
    return np.linalg.norm(q - (origin + t[:, None] * unit), axis=1)


def abort_clearance(
    mode: str,
    start: tuple[float, float, float],
    pad: tuple[float, float],
    others: dict[int, dict],
    show_elapsed_s: float | None,
    threshold_m: float = DEFAULT_LIMITS.design_separation_m,
    dt: float = ABORT_CLEARANCE_DT_S,
) -> dict:
    """Check an aborting drone's modelled descent against every other drone.

    Pure function over a snapshot, so it can run off the event loop and be
    tested without a fleet.

    Args:
        mode: One of :data:`ABORT_MODES`.
        start: Aborting drone's current fleet-frame ``(x, y, altitude)``.
        pad: Its own launch pad's fleet-frame ``(x, y)`` (RTL's destination).
        others: Drone id -> either ``{"waypoints": [...]}`` (its remapped show
            trajectory, NED z) or ``{"position": (x, y, altitude)}`` (holding
            still, e.g. no show playing).
        show_elapsed_s: Current show time the waypoints are sampled from, or
            ``None`` if no show is playing (waypoint-only entries then hold
            their first waypoint).
        threshold_m: Separation below which a pass is reported.
        dt: Sample interval, seconds.

    Returns:
        ``{"clear", "threshold_m", "mode", "model", "horizon_s", "conflicts"}``,
        conflicts being ``{"drone_id", "min_distance_m", "in_s"}`` sorted
        closest first. A pass is reported when a sampled distance is below
        ``threshold_m`` plus half of what the pair can close in one interval,
        so a closest approach falling between two samples isn't missed.
    """
    if mode not in ABORT_MODES:
        raise ValueError(f"mode must be one of {ABORT_MODES}, got {mode!r}")

    p0 = np.array(start, dtype=float)
    p0[2] = max(p0[2], 0.0)
    # Where the horizontal leg ends and the vertical one begins. LAND/ESTOP
    # have no horizontal leg, so it's the start point itself.
    corner = np.array([pad[0], pad[1], p0[2]]) if mode == "rtl" else p0.copy()
    horizontal = float(np.linalg.norm(corner - p0))
    vertical = float(p0[2])

    horizon = _abort_horizon(mode, horizontal, vertical)
    tau = np.array(sweep_times(horizon, dt))
    s_slow, s_fast = _abort_progress(mode, horizontal, vertical, tau)

    # Fastest the aborting drone can be moving during each interval, for the
    # between-samples pad.
    if mode == "estop":
        abort_speed = ABORT_FALL_ACCEL_RANGE_MS2[1] * (tau + dt)
    elif mode == "rtl":
        abort_speed = np.full_like(tau, max(ABORT_RTL_SPEED_RANGE_MS[1], ABORT_DESCENT_RATE_RANGE_MS[1]))
    else:
        abort_speed = np.full_like(tau, ABORT_DESCENT_RATE_RANGE_MS[1])

    leg1_unit = (corner - p0) / horizontal if horizontal > 0 else np.zeros(3)
    leg2_unit = np.array([0.0, 0.0, -1.0])
    # Which leg(s) the occupied stretch [s_slow, s_fast] touches at each sample.
    leg1_lo, leg1_hi = np.minimum(s_slow, horizontal), np.minimum(s_fast, horizontal)
    leg2_lo, leg2_hi = np.maximum(s_slow, horizontal) - horizontal, np.maximum(s_fast, horizontal) - horizontal
    on_leg1 = s_slow <= horizontal
    on_leg2 = s_fast >= horizontal

    conflicts = []
    for drone_id, other in others.items():
        if "waypoints" in other and other["waypoints"]:
            wps = other["waypoints"]
            times = np.array([w["time"] for w in wps], dtype=float)
            t = show_elapsed_s + tau if show_elapsed_s is not None else np.zeros_like(tau)
            # np.interp is interpolate_waypoint's position rule (linear inside
            # the span, clamped to the end waypoints outside it), vectorised
            # over every sample at once: a per-sample Python call is ~1000x
            # slower, and this must not stall the playback loop at fleet scale.
            q = np.column_stack([
                np.interp(t, times, [w["x"] for w in wps]),
                np.interp(t, times, [w["y"] for w in wps]),
                -np.interp(t, times, [w["z"] for w in wps]),  # NED down -> altitude
            ])
            if len(wps) > 1:
                seg = np.diff(np.array([[w["x"], w["y"], w["z"]] for w in wps], dtype=float), axis=0)
                span = np.diff(times)
                speeds = np.linalg.norm(seg, axis=1) / np.where(span > 0, span, np.inf)
                other_speed = float(speeds.max())
            else:
                other_speed = 0.0
        elif "position" in other:
            q = np.tile(np.array(other["position"], dtype=float), (len(tau), 1))
            other_speed = 0.0
        else:
            continue

        d1 = np.where(on_leg1, _distance_to_leg(q, p0, leg1_unit, leg1_lo, leg1_hi), np.inf)
        d2 = np.where(on_leg2, _distance_to_leg(q, corner, leg2_unit, leg2_lo, leg2_hi), np.inf)
        dist = np.minimum(d1, d2)
        limit = threshold_m + (other_speed + abort_speed) * dt / 2.0
        hits = dist < limit
        if np.any(hits):
            i = int(np.argmin(np.where(hits, dist, np.inf)))
            conflicts.append({
                "drone_id": drone_id,
                "min_distance_m": round(float(dist[i]), 2),
                "in_s": round(float(tau[i]), 1),
            })

    conflicts.sort(key=lambda c: c["min_distance_m"])
    return {
        "clear": not conflicts,
        "threshold_m": threshold_m,
        "mode": mode,
        "model": _ABORT_MODEL_DESCRIPTIONS[mode],
        "horizon_s": round(horizon, 1),
        "conflicts": conflicts,
    }


#: Evidence tier of each vehicle backend (issue #170). "kinematic" is the
#: vectorized tara_sim fleet: it scales to hundreds of drones but has no flight
#: dynamics, so it is scale evidence only. "px4-sitl" is real PX4 SITL under
#: Gazebo: real flight dynamics, but capped near 10 drones on one machine.
#: Results from the two must never be presented as one another.
FIDELITY_BY_BACKEND = {"sim": "kinematic", "px4": "px4-sitl"}


class DroneManager:
    # Heartbeat telemetry older than this is distrusted outright. Shared by
    # every staleness check in this class and, via this constant, by
    # /api/status and /api/sim/status in main.py - so the "connected"/"alive"
    # counts an operator reads match the same freshness bar flight-command
    # code actually enforces. Before this was shared, those two endpoints
    # only checked the transport-level `connected` flag: a drone whose
    # autopilot froze mid-session but kept its MAVSDK/UDP session open would
    # have read as "connected" there forever, even though every actual
    # flight-command path in this class would already be refusing it as
    # stale.
    HEARTBEAT_STALE_S = 2.5

    def __init__(
        self,
        ports: list[int] = [14540, 14541, 14542],
        backend: str = "px4",
        state_path: str = DEFAULT_STATE_PATH,
        broadcast_transmit=None,
        audit_path: str | None = DEFAULT_AUDIT_PATH,
        sim_rng_seed: int | None = None,
        sim_packet_loss_base: float = 0.0,
        sim_packet_loss_per_100m: float = 0.0,
    ):
        """
        Args:
            ports: UDP MAVLink ports, one per drone (backend="px4" only —
                length still determines fleet size when backend="sim", but the
                port *values* are unused since there is no real network layer).
            backend: "px4" (default) connects to real MAVSDK/PX4 SITL exactly
                as before. "sim" hands every drone a
                tara_sim.system.SimSystem view over one shared, vectorized
                tara_sim.FleetState instead — a single background tick task for
                the whole fleet rather than a mavsdk_server process and 8
                asyncio tasks per drone, which is what makes fleets of 100+
                practical on one machine. SimSystem duck-types the exact
                mavsdk.System surface this class calls, so the launch
                sequence, RTL, land, and the trajectory playback loop below
                run unchanged on either backend. Telemetry is the exception:
                on "sim" one bulk-ingestion task reads the whole fleet's
                arrays at once (issue #51) instead of 8 streaming tasks per
                drone.
            state_path: where the active show (trajectory + playback position)
                is persisted so a GCS restart can offer to resume it (issue #4).
                Overridable so tests don't collide on/pollute the real file.
            broadcast_transmit: called with ``(payload_bytes, target_system)``
                to put a show-trigger frame on the fleet radio — the same
                injected-transmit seam ``BroadcastManager`` uses. On
                backend="sim" it defaults to the sim fleet's shared radio. On
                backend="px4" there is no default: stock PX4 has no onboard
                show executor to act on the trigger (see #47), so onboard
                execution is refused until a radio and firmware are wired in.
            audit_path: JSONL file the fleet lifecycle state machine appends
                every transition to (issue #52). ``None`` keeps it in memory.
            sim_rng_seed: backend="sim" only - seeds tara_sim's physics/noise/
                RF-channel randomness for reproducible runs (issue #62).
                ``None`` (default) is nondeterministic, same as tara_sim's own
                default.
            sim_packet_loss_base: backend="sim" only - base packet-error rate
                for tara_sim's RF channel model (issue #62). ``0.0``
                (default) models no RF loss at all - the pre-#62 sim backend.
            sim_packet_loss_per_100m: backend="sim" only - additional PER per
                100m of distance from each drone's pad (issue #62).
        """
        if backend not in ("px4", "sim"):
            raise ValueError(f"backend must be 'px4' or 'sim', got {backend!r}")
        self.ports = ports
        self.backend = backend
        self._sim_rng_seed = sim_rng_seed
        self._sim_packet_loss_base = sim_packet_loss_base
        self._sim_packet_loss_per_100m = sim_packet_loss_per_100m
        # Evidence tier of this backend (issue #170); see FIDELITY_BY_BACKEND.
        self.fidelity = FIDELITY_BY_BACKEND[backend]
        self._sim_fleet = None  # tara_sim.SimFleet, created in connect_all() when backend == "sim"
        self.drones = {}  # drone_id -> System (real or SimSystem)
        self.telemetry = {}  # drone_id -> status dict
        self._tasks = []
        self.trajectory_task = None
        self.is_running_trajectory = False
        # Drone IDs with a single-drone bench/demo sequence (issue #131, #135)
        # currently in flight - concurrency guard so two overlapping requests
        # on the same drone can't both try to drive it (issue #145 review).
        self.single_drone_sequence_active: set[int] = set()
        self.geofence_radius = DEFAULT_LIMITS.geofence_radius_m  # hard RTL radius; recomputed per-show by _apply_geofence_envelope()
        self.soft_geofence_radius = None  # soft-warning radius; None (disabled) until a show sets one
        self.geofence_breaches = {}  # drone_id -> bool, tracks active hard geofence breaches (triggers RTL)
        self.geofence_warnings = {}  # drone_id -> bool, tracks active soft-warning state (logs only, no RTL)
        self.proximity_breaches = {}  # tracks active proximity breaches per drone pair: (id1, id2) -> bool
        self.active_trajectory = None  # active trajectory stored for verification and start
        # The trajectory actually being flown: active_trajectory after
        # remap_trajectory_to_healthy_drones, so keyed by physical drone id
        # rather than slot. The abort clearance check reads neighbours' future
        # positions from this — reading active_trajectory by drone id would
        # check against the wrong drone whenever a remap moved a slot.
        self.playing_trajectory: dict | None = None
        # Event-loop time playback started (after any trigger_epoch wait); None
        # while no show is being played. show_started_at is wall-clock and
        # includes that wait, so it can't give the show time itself.
        self._playback_start_time: float | None = None

        # Low-rate telemetry recording for post-flight CSV export (issue #8).
        # Independent of the 10Hz control/safety loops above.
        self.telemetry_log = []  # list of per-sample dicts, appended during a show
        self._telemetry_recording_task = None
        self.is_recording_telemetry = False

        # Show-state persistence (issue #4): periodically snapshot the active
        # trajectory + playback position so a crashed/restarted GCS can offer
        # to resume instead of starting blind.
        self.state_path = state_path
        self.show_mode = "LOCAL"
        self.show_started_at = None  # time.time() when the current trajectory began playing
        self.pending_resume_state = None  # loaded-but-not-yet-applied saved state, set by load_pending_resume_state()
        self._state_snapshot_task = None

        # Onboard show execution (issue #47): each drone is uploaded its own
        # compiled mission during staging, arming is gated on every one being
        # verified, and the show is started by one broadcast trigger instead
        # of a 10Hz unicast setpoint stream.
        self._broadcast_transmit = broadcast_transmit
        self.staged_show = None  # see upload_show()
        self._mission_dir = None  # local copies of compiled missions (FTP uploads from a file path)
        self._onboard_show_id = None  # show_id of an onboard show currently triggered, for CANCEL
        self.trigger_frames_sent = 0

        # Fleet lifecycle (issue #52). The API validates operator commands
        # against it; _lifecycle_monitor_loop advances it from telemetry.
        # context stamps backend/fidelity on every transition and rejection
        # (issue #170), same pattern as main.py's command_audit/preflight_engine.
        self.fleet_fsm = FleetStateMachine(
            audit_path=audit_path,
            context=lambda: {"backend": self.backend, "fidelity": self.fidelity},
        )
        self.launch_task = None
        # When the last launch task ended (monotonic seconds), or None. Lets the
        # lifecycle monitor give an arm that has not reached telemetry yet a
        # moment before calling the launch failed (see LAUNCH_ARM_GRACE_S).
        self._launch_finished_at: float | None = None

    async def connect_all(self) -> None:
        """Connects to all drones concurrently."""
        logging.info(f"Connecting to {len(self.ports)} drones (backend={self.backend}) on ports: {self.ports}")

        if self.backend == "sim":
            from tara_sim import RFChannelConfig, SimConfig, SimFleet

            sim_config = SimConfig(rng_seed=self._sim_rng_seed) if self._sim_rng_seed is not None else None
            rf_channel_config = (
                RFChannelConfig(
                    packet_loss_base=self._sim_packet_loss_base,
                    packet_loss_per_100m=self._sim_packet_loss_per_100m,
                )
                if self._sim_packet_loss_base > 0 or self._sim_packet_loss_per_100m > 0
                else None
            )
            self._sim_fleet = SimFleet(len(self.ports), config=sim_config, rf_channel_config=rf_channel_config)
            self._tasks.append(self._sim_fleet.start())
            if self._broadcast_transmit is None:
                self._broadcast_transmit = self._sim_fleet.broadcast

        connect_tasks = []
        for i, port in enumerate(self.ports):
            connect_tasks.append(self._connect_drone(i, port))

        # Use return_exceptions=True so that one failing drone doesn't break GCS startup
        results = await self._for_each_drone(connect_tasks, return_exceptions=True)
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logging.error(f"Failed to connect Drone {i} on port {self.ports[i]}: {res}")

        if self.backend == "sim":
            self._tasks.append(asyncio.create_task(self._bulk_telemetry_loop()))

        # Start proximity safety monitor loop
        self._tasks.append(asyncio.create_task(self._proximity_monitor_loop()))
        # Start geofence enforcement loop
        self._tasks.append(asyncio.create_task(self._geofence_monitor_loop()))
        # Advance the fleet lifecycle from telemetry (issue #52)
        self._tasks.append(asyncio.create_task(self._lifecycle_monitor_loop()))

    async def _for_each_drone(self, coros: list, return_exceptions: bool = False) -> list:
        """Run one coroutine per drone: concurrently on px4, in turn on sim.

        On px4 each call is a real gRPC round trip, so gather() overlaps them.
        On sim each is an in-process array write that never suspends, so
        gather() only buys a Task per drone: 1,000 of them at N=1000 every
        time this runs (issue #51), at twice the cost of awaiting in turn.
        """
        if self.backend != "sim":
            return await asyncio.gather(*coros, return_exceptions=return_exceptions)
        results = []
        for i, coro in enumerate(coros):
            try:
                results.append(await coro)
            except Exception as e:
                if not return_exceptions:
                    for pending in coros[i + 1:]:
                        pending.close()
                    raise
                results.append(e)
        return results

    async def _connect_drone(self, drone_id: int, port: int):
        try:
            if self.backend == "sim":
                drone = self._sim_fleet.system(drone_id)
                await drone.connect()
            else:
                # Use unique gRPC port for each drone to avoid conflicts
                drone = System(port=50051 + drone_id)
                await drone.connect(system_address=f"udpin://127.0.0.1:{port}")

            self.drones[drone_id] = drone
            self.telemetry[drone_id] = {
                "id": drone_id,
                "port": port,
                "connected": False,
                "armed": False,
                "flight_mode": "UNKNOWN",
                "battery_percent": 0.0,
                "battery_voltage": 0.0,
                "gps_lock": False,
                "satellites": 0,
                "gps_fix_type": "NO_GPS",
                "comm_latency_ms": 0.0,
                "home_lat": 0.0,
                "home_lon": 0.0,
                "home_alt": 0.0,
                "local_x": 0.0,
                "local_y": 0.0,
                "local_z": 0.0,
                "fleet_x": 0.0,
                "fleet_y": 0.0,
                "fleet_z": 0.0,
                "heading": 0.0,
                # Planned fleet-frame position for the current trajectory tick.
                # Only meaningful while planned_valid is True (see
                # _run_trajectory_loop); a parked fleet has no plan to deviate from.
                "planned_x": 0.0,
                "planned_y": 0.0,
                "planned_z": 0.0,
                "planned_valid": False,
                # Pre-arm checklist inputs (tara_gcs.preflight, issue #48).
                # time.monotonic() when the link last came up with no dropped
                # heartbeat or failed latency probe since; None while down.
                "link_stable_since": None,
                "health_armable": False,
                "health_home_ok": False,
                # Hash of the mission the vehicle holds onboard, as reported
                # by whatever uploaded it. None under streamed execution, where
                # the GCS sends setpoints and nothing is stored onboard.
                "mission_hash": None,
                # None, or {"type": "rtl"|"land"|"estop", "at": epoch seconds}
                # once this drone alone has been aborted out of the show (issue
                # #59). Cleared when the drone is explicitly re-launched.
                "aborted": None,
                "last_heartbeat": None,
                # MAVSDK LandedState as a string ("ON_GROUND", "IN_AIR", ...),
                # or None until the first sample arrives. Ground-contact
                # confirmation (issue #145) reads this directly rather than
                # inferring it from disarm alone (see
                # execute_single_drone_demo_sequence). landed_state_at is
                # when it was last written, so a stalled stream can be told
                # apart from a genuinely fresh ON_GROUND (issue #145 review).
                "landed_state": None,
                "landed_state_at": None,
            }

            # On the sim backend one fleet-wide task fills this in instead
            # (see _bulk_telemetry_loop, started by connect_all).
            if self.backend == "sim":
                return

            # Start telemetry loop for this drone
            self._tasks.append(asyncio.create_task(self._monitor_connection(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_battery(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_flight_mode(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_gps(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_position(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_armed_state(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_landed_state(drone_id)))
            self._tasks.append(asyncio.create_task(self._measure_latency_loop(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_home(drone_id)))
            self._tasks.append(asyncio.create_task(self._monitor_health(drone_id)))
        except Exception as e:
            logging.error(f"Error during _connect_drone initialization for Drone {drone_id}: {e}")
            raise e

    async def shutdown(self):
        """Cleans up all background monitoring tasks and disarms drones safely."""
        logging.info("Shutting down DroneManager...")

        # Stop telemetry recording if running
        self.stop_telemetry_recording()

        # If a show is active, persist one last snapshot before tearing down so
        # a restart can offer to resume from here (issue #4) — deliberately not
        # calling stop_trajectory()/discard_show_state() below, since shutdown
        # is not "the show ended," it's "GCS is going away mid-show."
        self.stop_state_snapshotting()
        self._write_state_snapshot()

        # Stop trajectory playback if running
        if self.trajectory_task:
            self.trajectory_task.cancel()
            try:
                await self.trajectory_task
            except asyncio.CancelledError:
                pass
            self.trajectory_task = None
            self.is_running_trajectory = False

        # Disarm active armed drones
        disarm_tasks = []
        for drone_id, drone in self.drones.items():
            if self.telemetry.get(drone_id, {}).get("armed", False):
                async def safe_disarm(d_id, d_obj):
                    try:
                        logging.info(f"Disarming Drone {d_id} during GCS shutdown...")
                        await d_obj.action.disarm()
                    except Exception as e:
                        logging.error(f"Failed to disarm Drone {d_id} on shutdown: {e}")
                disarm_tasks.append(safe_disarm(drone_id, drone))
        if disarm_tasks:
            await asyncio.gather(*disarm_tasks, return_exceptions=True)

        # Cancel telemetry & safety monitoring loops
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

        self.drones.clear()

        if self._mission_dir:
            shutil.rmtree(self._mission_dir, ignore_errors=True)
            self._mission_dir = None

    # --- Telemetry loops ---

    async def _bulk_telemetry_loop(self):
        """Fill every sim drone's telemetry from one vectorized read (issue #51).

        Replaces the 8 per-drone monitor tasks below on backend="sim", where
        all 8 would only be reading rows of the same shared numpy arrays. At
        1,000 drones those are 8,000 always-on tasks, enough to saturate the
        event loop on their own. This is one task, whatever the fleet size.
        """
        try:
            tick = 0
            while True:
                try:
                    self._ingest_bulk_telemetry(
                        self._sim_fleet.get_bulk_telemetry(),
                        include_slow=tick % SIM_SLOW_TELEMETRY_EVERY == 0,
                    )
                except Exception as e:
                    logging.error(f"Error in _bulk_telemetry_loop: {e}")
                tick += 1
                await asyncio.sleep(SIM_TELEMETRY_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def _ingest_bulk_telemetry(self, snap, include_slow: bool) -> None:
        """Write one BulkTelemetry snapshot into self.telemetry.

        Produces exactly what the per-drone monitors would: the same fields,
        the same rounding, the same fleet-frame shift, and the same geofence
        reset on disarm. The rows come out of the arrays in one ``tolist()``
        each, as plain Python floats and bools, so the dicts hold the same
        types as on the px4 backend. Rounding stays Python's ``round``:
        ``np.round`` rounds a decimal half differently (0.615 -> 0.62, where
        ``round`` gives 0.61), and the two backends should not disagree by a
        centimetre over which function was used.
        """
        # Deferred like the SimFleet import in connect_all - tara_sim stays an
        # optional dependency outside the sim backend.
        from tara_sim.fleet_state import LANDED_ALTITUDE_M

        ids = [d for d in self.telemetry if d < len(snap.armed)]
        if not ids:
            return
        idx = np.asarray(ids)
        positions = snap.position[idx].tolist()
        velocities = snap.velocity[idx].tolist()
        pads = _PAD_ARRAY[idx].tolist()
        connected = snap.connected[idx].tolist()
        armed = snap.armed[idx].tolist()

        for k, drone_id in enumerate(ids):
            tel = self.telemetry[drone_id]
            is_conn = bool(connected[k])
            if is_conn:
                tel["last_heartbeat"] = time.time()
                if tel["link_stable_since"] is None:
                    tel["link_stable_since"] = time.monotonic()
            else:
                tel["link_stable_since"] = None
            tel["connected"] = is_conn
            if hasattr(self, "_sim_fleet") and self._sim_fleet is not None:
                armable = bool(self._sim_fleet.state.is_armable(drone_id))
                tel["health_armable"] = armable
                tel["health_home_ok"] = armable
            north, east, alt = positions[k]
            local_x, local_y, local_z = round(north, 2), round(east, 2), round(alt, 2)
            tel["local_x"], tel["local_y"], tel["local_z"] = local_x, local_y, local_z
            tel["fleet_x"] = round(local_x + pads[k][0], 2)
            tel["fleet_y"] = round(local_y + pads[k][1], 2)
            tel["fleet_z"] = local_z
            # Mirrors FleetState._step's own ON_GROUND threshold (same
            # altitude cutoff, same vertical-speed deadband) and SimSystem's
            # per-drone landed_state() stream exactly, applied to the same
            # noisy position every other field here reports. Only ON_GROUND/
            # IN_AIR are modelled; the sim has no taking-off/landing phase.
            tel["landed_state"] = (
                "ON_GROUND" if alt <= LANDED_ALTITUDE_M and abs(velocities[k][2]) < 0.1 else "IN_AIR"
            )
            tel["landed_state_at"] = time.time()
            prev_armed = tel["armed"]
            tel["armed"] = armed[k]
            # Reset geofence breach tracking when drone disarms (as _monitor_armed_state does)
            if prev_armed and not armed[k] and drone_id in self.geofence_breaches:
                self.geofence_breaches[drone_id] = False
                logging.info(f"Drone {drone_id} disarmed — geofence breach state reset.")

        if not include_slow:
            return
        battery = snap.battery_pct[idx].tolist()
        voltage = snap.battery_voltage[idx].tolist()
        satellites = snap.satellites[idx].tolist()
        home_lat = snap.home_lat[idx].tolist()
        home_lon = snap.home_lon[idx].tolist()
        home_alt = snap.home_alt[idx].tolist()
        for k, drone_id in enumerate(ids):
            tel = self.telemetry[drone_id]
            tel["battery_percent"] = round(battery[k], 1)
            tel["battery_voltage"] = round(voltage[k], 2)
            tel["flight_mode"] = str(snap.flight_mode[drone_id])
            tel["satellites"] = satellites[k]
            tel["gps_lock"] = satellites[k] >= 6
            tel["gps_fix_type"] = str(snap.gps_fix_type[drone_id])
            tel["home_lat"] = home_lat[k]
            tel["home_lon"] = home_lon[k]
            tel["home_alt"] = home_alt[k]
            # The sim is in-process: there is no link whose round trip to
            # measure, which is also what the latency probe read here (~0ms).
            tel["comm_latency_ms"] = 0.0

    async def _monitor_connection(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for state in drone.core.connection_state():
                tel = self.telemetry[drone_id]
                if state.is_connected:
                    tel["last_heartbeat"] = time.time()
                    if tel["link_stable_since"] is None:
                        tel["link_stable_since"] = time.monotonic()
                elif not state.is_connected:
                    tel["link_stable_since"] = None
                tel["connected"] = state.is_connected
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_connection for Drone {drone_id}: {e}")

    async def _monitor_battery(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for battery in drone.telemetry.battery():
                # MAVSDK's remaining_percent is already in the 0-100 range, not a 0-1 fraction.
                self.telemetry[drone_id]["battery_percent"] = round(battery.remaining_percent, 1)
                self.telemetry[drone_id]["battery_voltage"] = round(battery.voltage_v, 2)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_battery for Drone {drone_id}: {e}")

    async def _monitor_flight_mode(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for fm in drone.telemetry.flight_mode():
                self.telemetry[drone_id]["flight_mode"] = str(fm)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_flight_mode for Drone {drone_id}: {e}")

    async def _monitor_gps(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for gps in drone.telemetry.gps_info():
                self.telemetry[drone_id]["satellites"] = gps.num_satellites
                # Arbitrary check for GPS lock
                self.telemetry[drone_id]["gps_lock"] = gps.num_satellites >= 6
                self.telemetry[drone_id]["gps_fix_type"] = str(gps.fix_type)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_gps for Drone {drone_id}: {e}")

    async def _monitor_position(self, drone_id):
        try:
            drone = self.drones[drone_id]
            pad_x_off, pad_y_off = _pad_offset(drone_id)
            async for pos in drone.telemetry.position_velocity_ned():
                # issue #145 review, item 1: on hardware, last_heartbeat was
                # only ever refreshed by _monitor_connection's
                # core.connection_state() stream, which (like landed_state(),
                # per Harsh's own SITL-cadence note) may only emit on an
                # actual state change rather than periodically - meaning a
                # connection that never flaps would go "stale" after 2.5s
                # regardless of how healthy the link actually is. This
                # telemetry stream only emits while MAVLink data is actually
                # arriving, so each tick is itself live-link evidence,
                # independent of connection_state()'s emission cadence.
                # TODO(SITL): confirm position_velocity_ned()'s real rate on
                # the target flight-controller stack before relying on this
                # as the primary heartbeat source in HARDWARE mode.
                self.telemetry[drone_id]["last_heartbeat"] = time.time()
                local_x = round(pos.position.north_m, 2)
                local_y = round(pos.position.east_m, 2)
                # NED: convert Down (negative) back to Altitude (positive) for frontend display
                local_z = round(-pos.position.down_m, 2)
                self.telemetry[drone_id]["local_x"] = local_x
                self.telemetry[drone_id]["local_y"] = local_y
                self.telemetry[drone_id]["local_z"] = local_z
                # Fleet frame: per-drone local position shifted into the shared
                # trajectory/fleet coordinate space (see LAUNCH_PAD_SPACING_Y_M above).
                self.telemetry[drone_id]["fleet_x"] = round(local_x + pad_x_off, 2)
                self.telemetry[drone_id]["fleet_y"] = round(local_y + pad_y_off, 2)
                self.telemetry[drone_id]["fleet_z"] = local_z
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_position for Drone {drone_id}: {e}")

    async def _monitor_armed_state(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for armed in drone.telemetry.armed():
                prev_armed = self.telemetry[drone_id]["armed"]
                self.telemetry[drone_id]["armed"] = armed
                # Reset geofence breach tracking when drone disarms
                if prev_armed and not armed:
                    if drone_id in self.geofence_breaches:
                        self.geofence_breaches[drone_id] = False
                        logging.info(f"Drone {drone_id} disarmed — geofence breach state reset.")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_armed_state for Drone {drone_id}: {e}")

    async def _monitor_landed_state(self, drone_id):
        """Tracks MAVSDK's own ground-contact state (issue #145).

        A prior version of the single-drone demo sequence inferred "landed"
        from disarm alone, which a mid-air disarm (fault, failsafe, a bug)
        would misreport as a normal touchdown. ``telemetry.landed_state()``
        is MAVSDK's actual vehicle-reported state (``ON_GROUND``, ``IN_AIR``,
        ``TAKING_OFF``, ``LANDING``), independent of the arm/disarm command
        path, so this is observed state, not an assumption.
        """
        try:
            drone = self.drones[drone_id]
            async for state in drone.telemetry.landed_state():
                self.telemetry[drone_id]["landed_state"] = str(state)
                self.telemetry[drone_id]["landed_state_at"] = time.time()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_landed_state for Drone {drone_id}: {e}")

    async def _measure_latency_loop(self, drone_id):
        try:
            drone = self.drones[drone_id]
            while True:
                if not self.telemetry[drone_id]["connected"]:
                    self.telemetry[drone_id]["comm_latency_ms"] = 0.0
                    await asyncio.sleep(1.0)
                    continue
                try:
                    start_time = asyncio.get_event_loop().time()
                    await drone.action.get_takeoff_altitude()
                    end_time = asyncio.get_event_loop().time()
                    latency_ms = round((end_time - start_time) * 1000.0, 1)
                    self.telemetry[drone_id]["comm_latency_ms"] = latency_ms
                except Exception:
                    self.telemetry[drone_id]["comm_latency_ms"] = -1.0
                    # A probe that got no answer is a dropped exchange: restart
                    # the heartbeat-continuity window the pre-arm check reads.
                    self.telemetry[drone_id]["link_stable_since"] = time.monotonic()
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _measure_latency_loop for Drone {drone_id}: {e}")

    async def _monitor_home(self, drone_id):
        try:
            drone = self.drones[drone_id]
            async for home in drone.telemetry.home():
                self.telemetry[drone_id]["home_lat"] = home.latitude_deg
                self.telemetry[drone_id]["home_lon"] = home.longitude_deg
                self.telemetry[drone_id]["home_alt"] = home.absolute_altitude_m
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_home for Drone {drone_id}: {e}")

    # --- Show staging: per-drone mission upload (issue #47) ---

    #: Drones uploaded concurrently. MAVLink FTP is chatty (many small
    #: acknowledged chunks), and on a shared radio a whole fleet uploading at
    #: once would collide itself into timeouts.
    UPLOAD_CONCURRENCY = 5
    UPLOAD_TIMEOUT_S = 60.0

    async def upload_show(self, trajectory_data: dict) -> dict:
        """Compile the show into one mission file per drone and upload each
        drone its own, verifying every copy against its CRC-32.

        Slots are assigned to healthy drones exactly as playback always has
        (remap_trajectory_to_healthy_drones), but here the assignment is fixed
        into the files themselves — each is compiled in its drone's own pad
        frame and addressed to that drone — so it is what the fleet will fly.
        Any change in fleet health afterwards means uploading again, which
        launch_gate() enforces.

        Returns:
            mission_status(): per-drone upload status and the launch gate.

        Raises:
            ValueError: If the show cannot be compiled (e.g. a position outside
                the mission format's range).
        """
        show_id = compute_show_id(trajectory_data)
        assignment = self.remap_trajectory_to_healthy_drones(trajectory_data)

        if self._mission_dir:
            shutil.rmtree(self._mission_dir, ignore_errors=True)
        self._mission_dir = tempfile.mkdtemp(prefix="tara-missions-")

        drones = {}
        local_paths = {}
        for drone_id, wps in assignment.items():
            blob = compile_mission(wps, drone_id=drone_id, show_id=show_id, origin=_pad_offset(drone_id))
            # MAVSDK's FTP upload keeps the local basename, so each drone's
            # file gets its own directory rather than its own name.
            local_dir = os.path.join(self._mission_dir, str(drone_id))
            os.makedirs(local_dir)
            local_paths[drone_id] = os.path.join(local_dir, MISSION_FILE_NAME)
            with open(local_paths[drone_id], "wb") as f:
                f.write(blob)
            drones[drone_id] = {
                "status": "PENDING",
                "bytes": len(blob),
                "crc32": f"{mission_crc32(blob):08x}",
                "msg": "",
            }

        excluded = {
            drone_id: "disconnected" if not tel["connected"] else "no trajectory slot (unhealthy, or surplus to the show)"
            for drone_id, tel in self.telemetry.items()
            if drone_id not in assignment
        }
        self.staged_show = {
            "show_id": show_id,
            "trajectory": assignment,
            "drones": drones,
            "excluded": excluded,
        }

        semaphore = asyncio.Semaphore(self.UPLOAD_CONCURRENCY)

        async def _bounded(drone_id):
            async with semaphore:
                await self._upload_mission(drone_id, local_paths[drone_id], drones[drone_id])

        await asyncio.gather(*(_bounded(drone_id) for drone_id in drones))

        verified = sum(1 for entry in drones.values() if entry["status"] == "VERIFIED")
        logging.info(
            f"Show {show_id.hex()[:8]} uploaded: {verified}/{len(drones)} missions verified, "
            f"{len(excluded)} drone(s) excluded."
        )
        return self.mission_status()

    @staticmethod
    async def _ensure_remote_dir(drone, remote_dir: str) -> None:
        """Make ``remote_dir`` exist on the drone's storage, or raise saying why not.

        MAVLink FTP's create_directory is not recursive, and on a fresh SITL
        rootfs or an empty SD card even ``/fs/microsd`` is missing (issue #172,
        F2), so each directory in the path is created in turn. PX4 reports a
        directory that already exists as an error too, so a create that raises
        proves nothing: the target is listed afterwards, and only a directory
        that is still not there fails the upload - with the directory and the
        last create error, not the bare FILE_DOES_NOT_EXIST the upload would
        otherwise end in.
        """
        current = ""
        last_error: Exception | None = None
        for part in (p for p in remote_dir.split("/") if p):
            current = f"{current}/{part}"
            try:
                await drone.ftp.create_directory(current)
            except Exception as e:
                last_error = e
        try:
            await drone.ftp.list_directory(remote_dir)
        except Exception as e:
            cause = f"; last create_directory error: {last_error}" if last_error else ""
            raise RuntimeError(
                f"mission directory {remote_dir} does not exist on the drone and could not be created "
                f"(listing it failed: {e}{cause})"
            ) from e

    async def _upload_mission(self, drone_id: int, local_path: str, entry: dict) -> None:
        """Upload one drone's mission over MAVLink FTP and have the drone
        confirm its copy's CRC-32. Records the outcome in ``entry``."""
        drone = self.drones[drone_id]
        remote_path = f"{MISSION_REMOTE_DIR}/{MISSION_FILE_NAME}"
        entry["status"] = "UPLOADING"

        async def _transfer() -> bool:
            await self._ensure_remote_dir(drone, MISSION_REMOTE_DIR)
            async for _ in drone.ftp.upload(local_path, MISSION_REMOTE_DIR):
                pass
            return await drone.ftp.are_files_identical(local_path, remote_path)

        try:
            identical = await asyncio.wait_for(_transfer(), timeout=self.UPLOAD_TIMEOUT_S)
        except Exception as e:
            entry["status"] = "FAILED"
            entry["msg"] = str(e) or type(e).__name__
            logging.error(f"Mission upload to Drone {drone_id} failed: {entry['msg']}")
            return

        if identical:
            entry["status"] = "VERIFIED"
            entry["msg"] = "Drone-reported CRC-32 matches the compiled mission."
        else:
            entry["status"] = "MISMATCH"
            entry["msg"] = "Drone's stored copy fails the CRC-32 comparison."
            logging.error(f"Mission on Drone {drone_id} does not match what was uploaded.")

    def launch_gate(self) -> dict:
        """Whether the fleet may arm: every connected drone must be accounted for
        by the current upload, and every drone given a mission must hold a
        verified copy of it and still be connected.

        Drones the upload deliberately left out (disconnected, unhealthy, or
        surplus to the show) hold no mission and are never armed, so they do
        not block the rest. A drone that connects *after* the upload does —
        the fleet has changed under the plan, and the plan is re-made by
        uploading again.

        Returns:
            ``{"ok", "reason", "armable", "problems"}``, where ``armable`` is
            the drones launch_sequence() may arm.
        """
        staged = self.staged_show
        if staged is None:
            reason = "No show has been uploaded to the fleet. Upload it (POST /api/upload-mission) before arming."
            return {"ok": False, "reason": reason, "armable": [], "problems": [reason]}

        problems = []
        if self.active_trajectory is not None:
            try:
                current_id = compute_show_id(self.active_trajectory)
            except ValueError:
                current_id = None
            if current_id != staged["show_id"]:
                problems.append("the loaded trajectory is not the show uploaded to the fleet — upload it again")

        armable = []
        for drone_id, tel in sorted(self.telemetry.items()):
            entry = staged["drones"].get(drone_id)
            if entry is None:
                if tel["connected"] and drone_id not in staged["excluded"]:
                    problems.append(f"drone {drone_id} connected after the upload and holds no mission")
                continue
            if not tel["connected"]:
                problems.append(f"drone {drone_id} holds a mission but is disconnected")
            elif entry["status"] != "VERIFIED":
                problems.append(f"drone {drone_id} mission {entry['status']}")
            else:
                armable.append(drone_id)

        if not staged["drones"]:
            problems.append("no drone was assigned a mission")

        ok = not problems
        reason = ""
        if not ok:
            shown = "; ".join(problems[:5])
            more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
            reason = f"Arming refused: {shown}{more}."
        return {"ok": ok, "reason": reason, "armable": armable, "problems": problems}

    def mission_status(self) -> dict:
        """Operator-facing summary of the staged show and the launch gate."""
        gate = self.launch_gate()
        staged = self.staged_show
        if staged is None:
            return {"staged": False, "launch_gate": gate}
        drones = staged["drones"]
        verified = sum(1 for entry in drones.values() if entry["status"] == "VERIFIED")
        return {
            "staged": True,
            "show_id": staged["show_id"].hex(),
            "assigned_count": len(drones),
            "verified_count": verified,
            "all_verified": bool(drones) and verified == len(drones),
            "drones": {str(drone_id): dict(entry) for drone_id, entry in sorted(drones.items())},
            "excluded": {str(drone_id): why for drone_id, why in sorted(staged["excluded"].items())},
            "launch_gate": gate,
        }

    async def _monitor_health(self, drone_id):
        """Mirrors the two health flags _launch_one waits on, for the pre-arm checklist."""
        try:
            drone = self.drones[drone_id]
            async for health in drone.telemetry.health():
                self.telemetry[drone_id]["health_armable"] = bool(health.is_armable)
                self.telemetry[drone_id]["health_home_ok"] = bool(health.is_home_position_ok)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.error(f"Error in _monitor_health for Drone {drone_id}: {e}")

    # --- Flight commands ---

    # Max drones armed/climbed concurrently in one launch batch. Keeps gRPC/CPU
    # load bounded as fleet size grows, instead of one drone at a time.
    LAUNCH_BATCH_SIZE = 5

    def start_launch_sequence(self, drone_ids: list[int] | None = None, require_ekf_health: bool = True) -> asyncio.Task:
        """Run launch_sequence() in the background and keep its task.

        The lifecycle monitor needs to know whether a launch is still in
        flight (PREARM with nobody armed is "still waiting" during a launch
        and "launch failed" after it), and it must know from the moment the
        API accepts the command, not from whenever the task first runs.
        """
        async def _run():
            try:
                await self.launch_sequence(drone_ids=drone_ids, require_ekf_health=require_ekf_health)
            except Exception as e:
                logging.error(f"Error during launch sequence execution: {e}")
            finally:
                self._launch_finished_at = time.monotonic()

        self._launch_finished_at = None
        self.launch_task = asyncio.create_task(_run())
        return self.launch_task

    def _launch_in_progress(self) -> bool:
        return self.launch_task is not None and not self.launch_task.done()

    def _launch_preempted(self, drone_id: int) -> bool:
        """True once LAND/RTL/ESTOP has superseded the launch in progress.

        Checked before every step that would put a drone further into the air,
        so an emergency during a launch doesn't get followed by the rest of the
        fleet arming and climbing.
        """
        if self.fleet_fsm.state in EMERGENCY_STATES:
            logging.warning(
                f"Drone {drone_id}: launch aborted — fleet is in {self.fleet_fsm.state.value}."
            )
            return True
        return False

    async def launch_sequence(self, drone_ids: list[int] | None = None, require_ekf_health: bool = True):
        """
        Arm, switch to offboard, and take off in bounded-concurrency batches,
        with each drone in a batch staggered by 1s (preserves the original
        spatial/temporal launch stagger without serializing the whole fleet).

        Idempotent: already-armed drones are skipped, so re-calling this to
        pick up stragglers (e.g. drones that missed the armable window) does
        not re-command drones that are already flying back to (0,0,0).

        Args:
            drone_ids: Launch only these drones (the set the pre-arm checklist
                cleared). Defaults to armable drones from the verified upload gate
                (if staged) or all connected drones.
            require_ekf_health: If False, skip waiting for is_armable/home lock
                and attempt to arm straight away. Only for an operator-confirmed
                EKF bypass on the bench (ENGINEERING profile, issue #48).

        Raises:
            LaunchGateError: If the gate does not pass.
        """
        if drone_ids is None:
            gate = self.launch_gate()
            if not gate["ok"]:
                raise LaunchGateError(gate["reason"])
            drone_ids = gate["armable"]
            # A fleet-wide launch skips aborted drones (issue #59): a
            # straggler-pickup relaunch mid-show must not re-arm an aborted
            # drone. The way to bring it back is an explicit per-drone launch:
            # naming an aborted drone in drone_ids is the explicit re-launch
            # that clears its aborted flag.
            aborted = [d_id for d_id in drone_ids if self._is_aborted(d_id)]
            if aborted:
                logging.warning(f"Launch: skipping aborted drones {aborted} — relaunch them explicitly by id.")
                drone_ids = [d_id for d_id in drone_ids if d_id not in aborted]
        else:
            drone_ids = [d for d in drone_ids if d in self.drones]
            if self.staged_show is not None:
                gate = self.launch_gate()
                if not gate["ok"]:
                    raise LaunchGateError(gate["reason"])
                drone_ids = [d for d in drone_ids if d in gate["armable"]]
        logging.info("Launching fleet (batched, concurrent)...")
        for batch_start in range(0, len(drone_ids), self.LAUNCH_BATCH_SIZE):
            batch = drone_ids[batch_start:batch_start + self.LAUNCH_BATCH_SIZE]
            await asyncio.gather(*(
                self._launch_one(drone_id, stagger_index=i, require_ekf_health=require_ekf_health)
                for i, drone_id in enumerate(batch)
            ))

    async def _launch_one(self, drone_id: int, stagger_index: int = 0, require_ekf_health: bool = True):
        """Arms, engages offboard, and climbs a single drone. Safe to re-call on an already-armed drone (no-op)."""
        if self.telemetry[drone_id]["armed"]:
            logging.info(f"Drone {drone_id} already armed — skipping (idempotent launch).")
            return

        drone = self.drones[drone_id]
        if not self.telemetry[drone_id]["connected"]:
            logging.warning(f"Skipping Drone {drone_id}: Disconnected")
            return

        # Clear the aborted flag if this drone is being explicitly relaunched
        # after a previous per-drone abort (issue #59). launch_sequence's
        # default drone list already skips aborted drones; arriving here means
        # the operator passed this id explicitly.
        if self._is_aborted(drone_id):
            self.telemetry[drone_id]["aborted"] = None
            logging.info(f"Drone {drone_id} re-launched — aborted flag cleared.")

        # Preserve the original per-drone launch stagger within a batch.
        await asyncio.sleep(stagger_index * 1.0)

        try:
            # Bypass magnetic and IMU calibration checks to prevent arming failure in simulation
            try:
                await drone.param.set_param_int("COM_ARM_MAG_STR", 0)
                await drone.param.set_param_int("EKF2_MAG_CHECK", 0)
                await drone.param.set_param_float("COM_ARM_IMU_ACC", 10.0)
                await drone.param.set_param_float("COM_ARM_IMU_GYR", 10.0)
            except Exception as e:
                logging.warning(f"Failed to set calibration bypass parameters for Drone {drone_id}: {e}")

            # Wait for drone to become armable AND for home position to latch
            # (bounded, so one stuck drone can't block the rest of the fleet —
            # and callers can safely re-invoke launch_sequence() to retry just
            # the stragglers).
            #
            # is_home_position_ok is checked alongside is_armable, not left
            # implicit: PX4 will report armable slightly before it has set a
            # home position, and arming in that window is the root of the
            # home-lock bug. A drone armed without home has no origin for RTL to
            # return to, and _monitor_home never receives a home message, so
            # telemetry's home_lat/home_lon stay 0.0 — which start_trajectory
            # treats as "not yet locked" and silently skips every GLOBAL-mode
            # setpoint for. The fleet appears to launch fine and then simply
            # does not fly the show.
            #
            # The simulator-side half of this fix is PX4_HOME_LAT/LON/ALT in
            # sim/px4/launch_fleet.sh, which gives SITL an origin to derive a
            # home position from in the first place.
            logging.info(f"Drone {drone_id} waiting to become armable with home lock...")
            async def _wait_armable():
                async for health in drone.telemetry.health():
                    if self.fleet_fsm.state in EMERGENCY_STATES:
                        break
                    if health.is_armable and health.is_home_position_ok:
                        logging.info(f"Drone {drone_id} is now armable with home position locked.")
                        break
            if not require_ekf_health:
                logging.warning(f"Drone {drone_id}: EKF health wait bypassed by operator override.")
            else:
                try:
                    await asyncio.wait_for(_wait_armable(), timeout=HOME_LOCK_TIMEOUT_S)
                except asyncio.TimeoutError:
                    logging.error(
                        f"Drone {drone_id} did not become armable with home lock within "
                        f"{HOME_LOCK_TIMEOUT_S}s — skipping (retry later via /api/launch)."
                    )
                    return

            if self._launch_preempted(drone_id):
                return

            # Arm
            try:
                await drone.action.arm()
                logging.info(f"Drone {drone_id} ARMED.")
            except Exception as e:
                logging.error(f"Arming Drone {drone_id} failed: {e}")
                return

            # Set initial position NED (0,0,0) before offboard
            try:
                await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, 0.0, 0.0))
            except Exception as e:
                logging.error(f"Setting initial position NED failed for Drone {drone_id}: {e}")
                return

            if self._launch_preempted(drone_id):
                return

            # Start offboard mode
            try:
                await drone.offboard.start()
                logging.info(f"Drone {drone_id} entered OFFBOARD mode.")
            except OffboardError as e:
                logging.error(f"Offboard start failed for Drone {drone_id}: {e._result.result}")
                await drone.action.disarm()
                return
            except Exception as e:
                logging.error(f"Offboard start failed for Drone {drone_id}: {e}")
                await drone.action.disarm()
                return

            if self._launch_preempted(drone_id):
                return

            # Ascend to target altitude (spatial separation)
            alt = _hover_altitude(drone_id)
            logging.info(f"Drone {drone_id} climbing to target hover altitude: {alt}m...")
            try:
                await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, -alt, 0.0))
            except Exception as e:
                logging.error(f"Climbing to target altitude failed for Drone {drone_id}: {e}")
                return

        except Exception as e:
            logging.error(f"Unexpected error launching Drone {drone_id}: {e}")

    async def trigger_rtl(self, operator: str = SYSTEM_OPERATOR, trigger: str = "rtl"):
        """Trigger RTL for all connected drones.

        Legal from every lifecycle state, so the transition can't fail; it's
        recorded first so the audit shows the RTL even if a vehicle rejects it.
        """
        self.fleet_fsm.apply(Command.RTL, operator=operator, trigger=trigger)
        print("FAILSAFE: Triggering Return to Launch (RTL) for all drones...")
        self.stop_trajectory()
        await self._for_each_drone([self._rtl_one(drone_id) for drone_id in list(self.drones)])

    async def trigger_land(self, operator: str = SYSTEM_OPERATOR, trigger: str = "land"):
        """Trigger Land for all connected drones. Legal from every lifecycle state."""
        self.fleet_fsm.apply(Command.LAND, operator=operator, trigger=trigger)
        print("Commanding all drones to Land...")
        self.stop_trajectory()
        await self._for_each_drone([self._land_one(drone_id) for drone_id in list(self.drones)])

    async def trigger_estop(self, operator: str = SYSTEM_OPERATOR, trigger: str = "estop"):
        """Kill every drone's motors immediately. Legal from every lifecycle state.

        This is flight termination: airborne drones fall. It exists for the
        case where a drone flying on is worse than a drone dropping (flyaway,
        toward the crowd), and it's LAND/RTL, not this, for anything else.
        """
        self.fleet_fsm.apply(Command.ESTOP, operator=operator, trigger=trigger)
        logging.critical("EMERGENCY STOP: killing motors on all drones.")
        self.stop_trajectory()
        await self._for_each_drone([self._estop_one(drone_id) for drone_id in list(self.drones)])

    # Per-drone command primitives, shared by the fleet-wide triggers above and
    # the single-drone abort below. Each returns None on success or the
    # vehicle's error message, and never raises: a fleet-wide RTL must still
    # reach every other drone when one of them rejects it.

    async def _rtl_one(self, drone_id: int) -> str | None:
        drone = self.drones[drone_id]
        try:
            await drone.offboard.stop()
        except Exception:
            pass  # Ignore if not in offboard
        try:
            await drone.action.return_to_launch()
            print(f"Drone {drone_id} commanded to RTL.")
            return None
        except Exception as e:
            print(f"Failed to command RTL on Drone {drone_id}: {e}")
            return str(e)

    async def _land_one(self, drone_id: int) -> str | None:
        drone = self.drones[drone_id]
        try:
            await drone.offboard.stop()
        except Exception:
            pass
        try:
            await drone.action.land()
            print(f"Drone {drone_id} commanded to Land.")
            return None
        except Exception as e:
            print(f"Failed to command Landing on Drone {drone_id}: {e}")
            return str(e)

    async def _estop_one(self, drone_id: int) -> str | None:
        """Kill one drone's motors. It falls: only for when flying on is worse.

        No offboard.stop() first, unlike RTL/LAND: kill works from any mode,
        and a round trip to leave offboard is delay an emergency stop can't
        afford.
        """
        try:
            await self.drones[drone_id].action.kill()
            logging.critical(f"Drone {drone_id} motors killed.")
            return None
        except Exception as e:
            logging.error(f"Failed to kill Drone {drone_id}: {e}")
            return str(e)

    def rf_channel_drop_rate(self) -> float | None:
        """Fleet-mean packet drop rate from tara_sim's RF channel model
        (issue #62), or ``None`` when there isn't one to report - backend is
        not "sim", the fleet hasn't connected yet, or no loss is configured.
        ``/api/status`` surfaces this directly; a scripted check or dashboard
        panel can poll it without knowing anything about tara_sim internals.
        """
        if self._sim_fleet is None:
            return None
        return self._sim_fleet.rf_channel_drop_rate()

    def is_heartbeat_fresh(self, tel: dict) -> bool:
        """True only when both the transport-level connection flag is set
        and a heartbeat has arrived within HEARTBEAT_STALE_S. The bar
        /api/status and /api/sim/status report "connected"/"alive" against -
        a drone whose connection flag is still set but whose autopilot
        stopped sending heartbeats (frozen, or a dead link that hasn't timed
        out the socket yet) does not count, matching what every
        flight-command staleness check in this class already enforces
        before doing anything else.
        """
        if not tel.get("connected", False):
            return False
        last_hb = tel.get("last_heartbeat")
        return last_hb is not None and (time.time() - last_hb) <= self.HEARTBEAT_STALE_S

    def _is_confirmed_on_ground(self, drone_id: int, tel: dict, context: str) -> bool:
        """Raises if connection/heartbeat/landed-state look stale; else
        reports whether the vehicle is confirmed ON_GROUND right now.
        Strict equality, no "unknown counts as landed" fallback (issue
        #145 review): an unconfirmed landed_state means wait, not assume
        grounded. A landed_state (or heartbeat) that stops updating while
        otherwise looking fine is its own stale-telemetry failure. A
        heartbeat or landed-state timestamp that was never set at all
        (None) is treated the same as stale, not as "no data yet, so
        don't flag it" - review follow-up: the old `is not None and ...`
        form let a telemetry field that had simply never been written
        pass silently.
        """
        if not tel.get("connected", False):
            raise RuntimeError(f"Drone {drone_id} disconnected {context}")
        last_hb = tel.get("last_heartbeat")
        if last_hb is None or (time.time() - last_hb) > self.HEARTBEAT_STALE_S:
            raise RuntimeError(f"Drone {drone_id} telemetry became stale {context}")
        landed_at = tel.get("landed_state_at")
        if landed_at is None or (time.time() - landed_at) > self.HEARTBEAT_STALE_S:
            raise RuntimeError(f"Drone {drone_id} landed-state telemetry became stale {context}")
        return tel.get("landed_state") == "ON_GROUND"

    async def _safe_recover(self, drone_id: int, drone, tel: dict, context: str) -> None:
        """Best-effort land+disarm after a mid-sequence failure (issue #145
        review, item 5 - and its own follow-up finding): once arm() has
        succeeded, a later step failing must not just raise and leave the
        vehicle armed and uncommanded. _land_one only *sends* the land
        command; it never waits for touchdown, so disarming right after it
        (the first version of this method) could disarm a drone that is
        still airborne. Waits for a fresh, confirmed ON_GROUND - the same
        confirmation the normal touchdown path uses - before disarming; if
        the deadline passes, leaves the land in progress and does not
        disarm. Swallows its own errors (and confirmed non-raising
        _land_one is still wrapped, on the same belt-and-suspenders
        reasoning the rest of this sequence uses) - the caller's original
        failure is what gets raised; this is a safety backstop, not the
        primary error path.
        """
        try:
            await self._land_one(drone_id)
        except Exception as e:
            logging.warning(f"Safe-recovery land attempt {context} for drone {drone_id}: {e}")
            return

        grounded = False
        deadline = time.time() + 10.0
        while time.time() < deadline:
            try:
                if self._is_confirmed_on_ground(drone_id, tel, f"during safe recovery ({context})"):
                    grounded = True
                    break
            except RuntimeError as e:
                logging.warning(f"Safe-recovery ground confirmation {context} for drone {drone_id}: {e}")
                return
            await asyncio.sleep(0.1)

        if not grounded:
            logging.warning(
                f"Safe-recovery {context} for drone {drone_id}: landing not confirmed within 10s "
                f"- leaving the land in progress, not disarming in flight."
            )
            return

        try:
            if tel.get("armed", False):
                await drone.action.disarm()
        except Exception as e:
            logging.warning(f"Safe-recovery disarm attempt {context} for drone {drone_id}: {e}")

    # issue #145 review (Harsh, real PX4 SITL + Gazebo, 2026-10-27): the
    # single-drone climb's stall detection. Class attributes (not method
    # locals) so tests can monkeypatch a short SINGLE_DRONE_CLIMB_STALL_WINDOW_S
    # instead of waiting out the real one.
    SINGLE_DRONE_CLIMB_STALL_WINDOW_S = 10.0  # > the worst liftoff delay observed (7.5s)
    SINGLE_DRONE_CLIMB_LIFTOFF_MARGIN_S = 15.0
    SINGLE_DRONE_CLIMB_MIN_RATE_M_S = 0.15  # conservative floor under the ~0.2 m/s observed on real PX4

    async def execute_single_drone_demo_sequence(
        self,
        drone_id: int = 0,
        target_alt_m: float = 1.0,
        hover_s: float = 5.0,
        operator: str = "pilot",
    ) -> list[dict]:
        """
        Executes a controlled single-drone hover and landing sequence for V1 demo proof.
        Sequence:
          1. Pre-check: Verify drone is connected, telemetry is fresh, and armable.
          2. Arm Drone and verify armed state from fresh telemetry.
          3. Offboard climb to target_alt_m (default 1.0m hover).
          4. Hover dwell for hover_s (default 5.0s).
          5. Landing: Commanded via autopilot land mode.
          6. Disarm: Verified via fresh observed telemetry.
        Returns a timestamped list of verified progression steps.
        """
        if drone_id not in self.drones:
            raise RuntimeError(f"Drone {drone_id} not available")
        drone = self.drones[drone_id]
        tel = self.telemetry.get(drone_id)
        if not tel or not tel.get("connected"):
            raise RuntimeError(f"Drone {drone_id} is disconnected")

        # Telemetry freshness check: heartbeat must be fresh
        now = time.time()
        last_hb = tel.get("last_heartbeat")
        if last_hb is None or (now - last_hb) > self.HEARTBEAT_STALE_S:
            raise RuntimeError(f"Drone {drone_id} telemetry is stale or missing heartbeat")

        steps = []
        t0 = time.time()
        steps.append({"step": "PREFLIGHT", "status": "PASSED", "time": t0, "drone_id": drone_id})

        # 1. Arm
        try:
            await drone.action.arm()
        except Exception as e:
            steps.append({"step": "ARM", "status": f"FAILED: {e}", "time": time.time()})
            # arm() raising doesn't rule out a partial arm (the command could
            # have been accepted with the ack lost) - _safe_recover is a
            # harmless no-op if it wasn't (issue #145 review follow-up).
            await self._safe_recover(drone_id, drone, tel, context="after arm() raised")
            raise RuntimeError(f"Arming failed: {e}")

        # Await observed telemetry confirmation of arming — NEVER mutate tel["armed"]!
        arm_confirmed = False
        arm_deadline = time.time() + 3.0
        while time.time() < arm_deadline:
            if not tel.get("connected", False):
                raise RuntimeError(f"Drone {drone_id} disconnected while awaiting arm confirmation")
            last_hb = tel.get("last_heartbeat")
            if last_hb is None or (time.time() - last_hb) > self.HEARTBEAT_STALE_S:
                raise RuntimeError(f"Drone {drone_id} telemetry became stale while awaiting arm confirmation")
            if tel.get("armed", False):
                arm_confirmed = True
                break
            await asyncio.sleep(0.05)

        if not arm_confirmed:
            steps.append({"step": "ARM", "status": "FAILED: arm not observed in telemetry", "time": time.time()})
            # arm() itself raised nothing, so the vehicle may actually be
            # armed even though we never observed it (issue #145 review,
            # item 5): recover rather than leave it armed and uncommanded.
            await self._safe_recover(drone_id, drone, tel, context="after an unconfirmed arm")
            raise RuntimeError(f"Arming could not be confirmed by telemetry for Drone {drone_id}")

        steps.append({"step": "ARM", "status": "ARMED", "time": time.time()})

        # From here on the vehicle is armed and may be flying: every exit path
        # below must either yield to an already-in-progress fleet emergency
        # (issue #145 review, B3 - EMERGENCY_STATES checks inside each phase)
        # or attempt a safe recovery, never just raise and leave it
        # uncommanded. This outer wrapper covers cancellation specifically
        # (B4): CancelledError is a BaseException, not an Exception, so none
        # of the `except Exception` blocks inside this method ever see it -
        # without this, cancelling mid-sequence left the vehicle flying with
        # no recovery attempt at all, in whatever state it was in when the
        # task stopped.
        try:
            await self._run_single_drone_flight_phase(drone_id, drone, tel, target_alt_m, hover_s, steps)
        except asyncio.CancelledError:
            if not self._emergency_in_progress():
                await self._safe_recover(drone_id, drone, tel, context="after the sequence was cancelled")
            raise

        return steps

    def _check_operator_takeover(self, drone_id: int, tel: dict, offboard_seen: bool) -> bool:
        """Returns whether OFFBOARD has been observed; raises OperatorTakeoverError
        once the vehicle leaves it (issue #171).

        Real PX4 SITL: a MANUAL_CONTROL stream plus a switch to POSCTL took the
        vehicle out of OFFBOARD mid-dwell, and the sequence - which only
        watched altitude, link and the fleet FSM - kept dwelling, then landed
        over the pilot. Only a change *away from* a mode this sequence has
        itself seen counts: PX4 can still report HOLD/TAKEOFF for a moment
        after arm, before the offboard request takes effect.
        """
        mode = tel.get("flight_mode")
        if mode == "OFFBOARD":
            return True
        if offboard_seen and mode is not None:
            raise OperatorTakeoverError(
                f"Drone {drone_id}: operator took over (flight mode left OFFBOARD for {mode}); "
                "the sequence is yielding without commanding the vehicle"
            )
        return offboard_seen

    def _emergency_in_progress(self) -> bool:
        """True once the fleet FSM has already moved to an emergency state
        (RTL/LAND/ESTOP) - issue #145 review, B3: something else is already
        commanding this drone by then, so the single-drone sequence must stop
        issuing its own commands rather than racing or duplicating them."""
        return self.fleet_fsm.state in EMERGENCY_STATES

    async def _run_single_drone_flight_phase(
        self, drone_id: int, drone, tel: dict, target_alt_m: float, hover_s: float, steps: list[dict],
    ) -> None:
        """Climb, hold and land - split out of execute_single_drone_demo_sequence
        so its caller can wrap just this part (everything after a confirmed
        arm) in one CancelledError handler. See that method's docstring."""
        # 2. Enter offboard and climb to target altitude, confirmed by
        # observed altitude rather than a fixed sleep (issue #145 review,
        # item 3): a fixed delay says nothing about whether the climb
        # actually reached, overshot or stalled short of the target.
        #
        # issue #145 review (Harsh, first real PX4 SITL + Gazebo run,
        # 2026-10-27): the original deadline (target_alt_m / 0.5 + 5.0s)
        # assumed climbing starts immediately at 0.5 m/s. Real PX4 spends
        # ~4.6-7.5s on arm/spool-up/takeoff ramp before it moves at all, then
        # climbs at ~0.2 m/s - tara_sim has no such delay and climbs faster,
        # which is why CI never caught this. A 1m/5s-hold request failed
        # twice ("observed 0.80 m", "observed 0.74 m") purely because the
        # deadline ran out while the vehicle was still genuinely climbing.
        #
        # Replaced with stall detection: keep waiting as long as altitude is
        # still making progress toward the target, fail only once there has
        # been no progress for SINGLE_DRONE_CLIMB_STALL_WINDOW_S - bounded by
        # a generous overall ceiling so a vehicle that never lifts off at all
        # still fails closed. The stall window is longer than the worst
        # liftoff delay observed (7.5s) specifically so the legitimate
        # pre-liftoff pause is never itself mistaken for a stall.
        alt_tolerance_m = max(0.15, 0.1 * target_alt_m)
        stall_window_s = self.SINGLE_DRONE_CLIMB_STALL_WINDOW_S
        climb_ceiling_s = (
            self.SINGLE_DRONE_CLIMB_LIFTOFF_MARGIN_S
            + target_alt_m / self.SINGLE_DRONE_CLIMB_MIN_RATE_M_S
        )
        try:
            await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, 0.0, 0.0))
            await drone.offboard.start()
            await drone.offboard.set_position_ned(PositionNedYaw(0.0, 0.0, -target_alt_m, 0.0))
            hover_reached = False
            climb_start = time.time()
            best_z = tel.get("local_z", 0.0)
            best_z_at = climb_start
            offboard_seen = False
            while time.time() - climb_start < climb_ceiling_s:
                if self._emergency_in_progress():
                    raise RuntimeError(
                        f"Drone {drone_id}: yielding to fleet emergency state "
                        f"{self.fleet_fsm.state.value} instead of continuing the climb"
                    )
                offboard_seen = self._check_operator_takeover(drone_id, tel, offboard_seen)
                if not tel.get("connected", False):
                    raise RuntimeError(f"Drone {drone_id} disconnected during climb")
                last_hb = tel.get("last_heartbeat")
                if last_hb is None or (time.time() - last_hb) > self.HEARTBEAT_STALE_S:
                    raise RuntimeError(f"Drone {drone_id} telemetry became stale during climb")
                current_z = tel.get("local_z", 0.0)
                if abs(current_z - target_alt_m) <= alt_tolerance_m:
                    hover_reached = True
                    break
                # A small deadband against ordinary telemetry noise, so tiny
                # jitter around a genuinely stalled altitude doesn't keep
                # resetting the stall clock.
                if current_z > best_z + 0.02:
                    best_z = current_z
                    best_z_at = time.time()
                elif time.time() - best_z_at > stall_window_s:
                    raise RuntimeError(
                        f"Drone {drone_id} climb stalled: no altitude progress toward "
                        f"{target_alt_m} m for {stall_window_s:.0f}s (observed {current_z:.2f} m)"
                    )
                await asyncio.sleep(0.1)
            if not hover_reached:
                raise RuntimeError(
                    f"Drone {drone_id} did not reach {target_alt_m} m within "
                    f"{climb_ceiling_s:.1f}s (observed {tel.get('local_z', 0.0):.2f} m)"
                )
            steps.append({
                "step": "HOVER_REACHED",
                "status": "HOLDING",
                "altitude_m": round(tel.get("local_z", 0.0), 2),
                "time": time.time(),
            })
        except Exception as e:
            steps.append({"step": "HOVER", "status": f"FAILED: {e}", "time": time.time()})
            # issue #145 review, B3: an emergency handler (RTL/LAND/ESTOP) is
            # already commanding this drone by now - recovering here too
            # would race or duplicate its commands.
            if not self._emergency_in_progress() and not isinstance(e, OperatorTakeoverError):
                await self._safe_recover(drone_id, drone, tel, context="after a failed climb")
            raise RuntimeError(f"Hover climb failed: {e}")

        # 3. Hold hover - dwell for the actual requested hover_s, watching for
        # disconnection or stale telemetry throughout rather than blindly
        # sleeping through it (issue #145 review, item 4): a drone that
        # dropped link mid-dwell was previously indistinguishable from one
        # holding steady.
        #
        # The altitude band here is deliberately looser than HOVER_REACHED's
        # alt_tolerance_m: that one judges a single instant right after an
        # active climb, this one has to tolerate ordinary hover noise over
        # the whole dwell without false-failing a real hold. It still catches
        # the actual failure mode (drifting or sinking away from the target),
        # just not fine positional noise.
        dwell_alt_tolerance_m = max(0.4, 0.3 * target_alt_m)
        dwell_start = time.time()
        try:
            while time.time() - dwell_start < hover_s:
                if self._emergency_in_progress():
                    raise RuntimeError(
                        f"Drone {drone_id}: yielding to fleet emergency state "
                        f"{self.fleet_fsm.state.value} instead of continuing the hover dwell"
                    )
                offboard_seen = self._check_operator_takeover(drone_id, tel, offboard_seen)
                if not tel.get("connected", False):
                    raise RuntimeError(f"Drone {drone_id} disconnected during hover dwell")
                last_hb = tel.get("last_heartbeat")
                if last_hb is None or (time.time() - last_hb) > self.HEARTBEAT_STALE_S:
                    raise RuntimeError(f"Drone {drone_id} telemetry became stale during hover dwell")
                # Re-checked every tick, not just once at HOVER_REACHED
                # (issue #145 review): drift or a slow sink mid-hover would
                # otherwise go unnoticed for the whole dwell.
                if abs(tel.get("local_z", 0.0) - target_alt_m) > dwell_alt_tolerance_m:
                    raise RuntimeError(
                        f"Drone {drone_id} drifted outside the hover altitude band during dwell "
                        f"(observed {tel.get('local_z', 0.0):.2f} m, target {target_alt_m} m)"
                    )
                await asyncio.sleep(0.1)
        except Exception as e:
            steps.append({"step": "HOVER_HOLD", "status": f"FAILED: {e}", "time": time.time()})
            # issue #145 review, B3: same reasoning as the climb phase above -
            # don't race or duplicate an emergency handler's own commands.
            if not self._emergency_in_progress() and not isinstance(e, OperatorTakeoverError):
                await self._safe_recover(drone_id, drone, tel, context="after a failed hover dwell")
            raise RuntimeError(f"Hover dwell failed: {e}")
        actual_dwell = round(time.time() - dwell_start, 2)
        steps.append({
            "step": "HOVER_HOLD_COMPLETE",
            "status": "STABLE",
            "dwell_s": actual_dwell,
            "time": time.time(),
        })

        # 4. Landing and confirmed touchdown/disarm
        try:
            if self._emergency_in_progress():
                raise RuntimeError(
                    f"Drone {drone_id}: yielding to fleet emergency state "
                    f"{self.fleet_fsm.state.value} instead of commanding its own landing"
                )
            self._check_operator_takeover(drone_id, tel, offboard_seen)
            err = await self._land_one(drone_id)
            if err:
                # issue #145 review, B2: offboard.stop() has already run
                # inside _land_one by this point, so a rejected land leaves
                # the vehicle neither in offboard nor landing - raising here
                # reaches the except below, which now attempts recovery.
                raise RuntimeError(f"Autopilot rejected land command: {err}")
            steps.append({
                "step": "LANDING_COMMANDED",
                "status": "LANDING",
                "landing_control": "AUTOPILOT_DELEGATED",
                "note": "Descent rate is governed by autopilot landing parameters",
                "time": time.time(),
            })

            # Check fresh telemetry observations for landed and disarmed state
            # Never fabricate or force tel["armed"] = False!
            disarm_confirmed = False
            land_deadline = time.time() + 10.0
            while time.time() < land_deadline:
                if not tel.get("armed", True) and self._is_confirmed_on_ground(
                    drone_id, tel, "while awaiting landing/disarm confirmation"
                ):
                    disarm_confirmed = True
                    break
                await asyncio.sleep(0.1)

            # issue #145 review, B1: this fallback used to call disarm()
            # unconditionally once the window above ran out - the same
            # in-flight-disarm risk _safe_recover was already fixed for, left
            # in this separate path. Only attempt it once the vehicle is
            # actually confirmed on the ground.
            if not disarm_confirmed and self._is_confirmed_on_ground(
                drone_id, tel, "before the fallback disarm attempt"
            ):
                try:
                    await drone.action.disarm()
                except Exception as e:
                    logging.warning(f"Explicit disarm attempt: {e}")

                # Poll again for observed telemetry disarm — NEVER mutate tel["armed"]
                post_disarm_deadline = time.time() + 2.0
                while time.time() < post_disarm_deadline:
                    if not tel.get("armed", True) and self._is_confirmed_on_ground(
                        drone_id, tel, "during disarm confirmation"
                    ):
                        disarm_confirmed = True
                        break
                    await asyncio.sleep(0.1)

            if not disarm_confirmed:
                steps.append({"step": "TOUCHDOWN_DISARM", "status": "FAILED: disarm not confirmed by telemetry", "time": time.time()})
                raise RuntimeError("Touchdown disarm could not be confirmed by telemetry")

            steps.append({"step": "TOUCHDOWN_DISARM", "status": "DISARMED", "time": time.time()})
        except Exception as e:
            steps.append({"step": "LANDING", "status": f"FAILED: {e}", "time": time.time()})
            # issue #145 review, B2: a rejected land (or any other failure in
            # this block) used to just raise with no recovery attempt at all.
            if not self._emergency_in_progress() and not isinstance(e, OperatorTakeoverError):
                await self._safe_recover(drone_id, drone, tel, context="after a failed landing")
            raise RuntimeError(f"Landing failed: {e}")

    # --- Single-drone abort (issue #59) ---

    def _is_aborted(self, drone_id: int) -> bool:
        return bool(self.telemetry.get(drone_id, {}).get("aborted"))

    def _show_elapsed(self) -> float | None:
        """Current show time, seconds, or None if no show is playing."""
        if not self.is_running_trajectory or self._playback_start_time is None:
            return None
        return asyncio.get_event_loop().time() - self._playback_start_time

    def _abort_clearance_inputs(self, drone_id: int) -> dict:
        """Snapshot everything abort_clearance needs, before the drone moves.

        Neighbours are the other connected, armed, non-aborted drones. While a
        show plays, a neighbour with a trajectory is checked against its
        planned future positions; otherwise (no show, or it has no slot) it's
        assumed to hold where telemetry last put it.
        """
        tel = self.telemetry[drone_id]
        elapsed = self._show_elapsed()
        others = {}
        for d_id, t in self.telemetry.items():
            if d_id == drone_id or not t.get("connected") or not t.get("armed") or self._is_aborted(d_id):
                continue
            if elapsed is not None and self.playing_trajectory and self.playing_trajectory.get(d_id):
                others[d_id] = {"waypoints": self.playing_trajectory[d_id]}
            else:
                others[d_id] = {"position": (t.get("fleet_x", 0.0), t.get("fleet_y", 0.0), t.get("fleet_z", 0.0))}
        return {
            "start": (tel.get("fleet_x", 0.0), tel.get("fleet_y", 0.0), tel.get("fleet_z", 0.0)),
            "pad": _pad_offset(drone_id),
            "others": others,
            "show_elapsed_s": elapsed,
        }

    async def abort_drone(self, drone_id: int, mode: str) -> dict:
        """Take one drone out of the show (RTL, LAND or ESTOP); the rest fly on.

        Unlike trigger_rtl/trigger_land this does not call stop_trajectory():
        playback, telemetry recording and the saved show all carry on for the
        other drones. The drone is flagged aborted first, so the playback loop
        stops commanding it before the abort command goes out, and stays
        aborted until it's explicitly relaunched (see launch_sequence).

        The clearance check runs after the command has been sent, from a
        snapshot taken before it: it is a warning, and must never delay the
        abort. It runs in a worker thread so a large fleet's sweep doesn't
        stall everyone else's setpoints.

        Aborting an already-aborted drone re-commands it, so an RTL can be
        escalated to an ESTOP.

        Returns:
            ``{"drone_id", "mode", "commanded", "error", "clearance", "warning"}``.
            ``commanded`` is False if the vehicle rejected the command;
            ``warning`` is a one-line summary of any clearance conflict.

        Raises:
            KeyError: ``drone_id`` isn't a connected drone.
            ValueError: ``mode`` isn't one of ABORT_MODES.
        """
        if mode not in ABORT_MODES:
            raise ValueError(f"mode must be one of {ABORT_MODES}, got {mode!r}")
        if drone_id not in self.drones:
            raise KeyError(drone_id)

        inputs = self._abort_clearance_inputs(drone_id)

        tel = self.telemetry[drone_id]
        tel["aborted"] = {"type": mode, "at": time.time()}
        tel["planned_valid"] = False
        logging.warning(f"ABORT: Drone {drone_id} aborting out of the show ({mode.upper()}); the rest of the fleet continues.")

        command = {"rtl": self._rtl_one, "land": self._land_one, "estop": self._estop_one}[mode]
        error = await command(drone_id)

        try:
            clearance = await asyncio.to_thread(abort_clearance, mode, **inputs)
        except Exception as e:
            # The abort has already gone out; a broken check must not turn it
            # into an error response.
            logging.error(f"ABORT: clearance check failed for Drone {drone_id}: {e}")
            clearance = None

        warning = None
        if clearance is None:
            warning = "Clearance check could not be run; descent path unverified."
        elif not clearance["conflicts"]:
            logging.info(f"ABORT: Drone {drone_id} {mode.upper()} path clear of all other drones (model: {clearance['model']}).")
        else:
            nearest = ", ".join(
                f"Drone {c['drone_id']} ({c['min_distance_m']:.2f}m in {c['in_s']:.1f}s)"
                for c in clearance["conflicts"][:5]
            )
            warning = (
                f"Modelled {mode.upper()} path of Drone {drone_id} passes within "
                f"{clearance['threshold_m']}m of {len(clearance['conflicts'])} drone(s): {nearest}."
            )
        if warning:
            logging.warning(f"ABORT: {warning}")

        return {
            "drone_id": drone_id,
            "mode": mode,
            "commanded": error is None,
            "error": error,
            "clearance": clearance,
            "warning": warning,
        }

    async def inject_fault(self, drone_id: int, fault_type: str, params: dict) -> dict:
        """Simulate a sensor/actuator fault on one drone (issue #63).

        backend="sim" only: every fault type is a direct edit of
        `tara_sim.FleetState`'s own arrays (``rtk_loss``, ``battery_drop``,
        ``deviation``) except ``motor_failure``, which instead drives the
        *real* single-drone abort path (`abort_drone`, mode="land") — a
        motor failure's correct response is the actual emergency landing
        sequence, not a simulated sensor reading, so there is nothing to
        fake at the tara_sim layer for it.

        Every existing monitor (the GNSS-fix preflight gate, the proximity/
        geofence sweep, the dashboard's own telemetry-driven HUD) reads
        these same arrays already — nothing downstream needs to know a
        fault was injected rather than arrived at physically, the same
        reasoning #62's RF channel model used for the offboard-loss
        failsafe.

        Args:
            drone_id: Which drone.
            fault_type: One of ``"rtk_loss"``, ``"battery_drop"``,
                ``"motor_failure"``, ``"deviation"``.
            params: Fault-specific. ``rtk_loss``: ``{"fix_type": str}``, one
                of ``tara_gcs.preflight.GNSS_FIX_TYPES``. ``battery_drop``:
                ``{"pct": float}``, 0-100. ``deviation``:
                ``{"offset_m": [north, east, alt]}``, 3 floats. Ignored for
                ``motor_failure``.

        Returns:
            A result dict; shape depends on ``fault_type`` (``motor_failure``
            returns exactly `abort_drone`'s own return shape).

        Raises:
            ValueError: backend is not "sim", the fleet hasn't connected
                yet, ``fault_type`` is unrecognised, or ``params`` fails
                that fault type's own validation.
            KeyError: ``drone_id`` isn't a connected drone.
        """
        if self.backend != "sim" or self._sim_fleet is None:
            raise ValueError("fault injection is only available on backend=\"sim\" with a connected fleet")
        if drone_id not in self.drones:
            raise KeyError(drone_id)

        if fault_type == "motor_failure":
            return await self.abort_drone(drone_id, "land")

        fs = self._sim_fleet.state
        if fault_type == "rtk_loss":
            fix_type = params.get("fix_type")
            if fix_type not in GNSS_FIX_TYPES:
                raise ValueError(f"fix_type must be one of {sorted(GNSS_FIX_TYPES)}, got {fix_type!r}")
            fs.inject_gnss_fault(drone_id, fix_type)
            return {"drone_id": drone_id, "fault_type": fault_type, "fix_type": fix_type}
        elif fault_type == "battery_drop":
            pct = params.get("pct")
            if not isinstance(pct, (int, float)):
                raise ValueError(f"pct must be a number, got {pct!r}")
            fs.inject_battery_fault(drone_id, float(pct))
            return {"drone_id": drone_id, "fault_type": fault_type, "pct": pct}
        elif fault_type == "deviation":
            offset_m = params.get("offset_m")
            if not (isinstance(offset_m, (list, tuple)) and len(offset_m) == 3):
                raise ValueError(f"offset_m must be [north, east, alt], got {offset_m!r}")
            fs.inject_position_deviation(drone_id, tuple(float(v) for v in offset_m))
            return {"drone_id": drone_id, "fault_type": fault_type, "offset_m": offset_m}
        else:
            raise ValueError(
                f"fault_type must be one of 'rtk_loss', 'battery_drop', 'motor_failure', 'deviation', got {fault_type!r}"
            )

    # --- Trajectory Playback ---

    def _apply_geofence_envelope(self, trajectory_data: dict) -> None:
        """Derives this show's soft/hard geofence radii from its full trajectory.

        Recomputed here from the trajectory's own waypoints (issue #53) via
        `tara_core.Show`, covering entry-blend + show + exit — not just launch
        pads, since a formation can carry a drone further from home mid-show.
        Deliberately recomputed rather than trusted from any client-supplied
        `_geofence` metadata field (e.g. one baked into an exported show by
        `tara_studio.exporter.export_json`): the runtime abort threshold must
        always match the geometry this GCS is actually about to fly, not a
        number that travelled alongside it and could be stale or wrong.

        Falls back to `DEFAULT_LIMITS.geofence_radius_m` (static, soft warning
        disabled) if the trajectory can't be parsed into a `Show` — e.g. an
        empty trajectory — so the hard RTL boundary is never left unset.
        """
        try:
            envelope = Show.from_any(trajectory_data).geofence_envelope()
        except (ValueError, KeyError) as e:
            logging.warning(
                f"Could not compute geofence envelope from trajectory ({e}); "
                f"falling back to default radius ({DEFAULT_LIMITS.geofence_radius_m}m)."
            )
            self.geofence_radius = DEFAULT_LIMITS.geofence_radius_m
            self.soft_geofence_radius = None
            return

        self.soft_geofence_radius = envelope["soft_warning_radius_m"]
        self.geofence_radius = envelope["hard_geofence_radius_m"]
        logging.info(
            f"Geofence envelope computed: trajectory max radius "
            f"{envelope['envelope_radius_m']:.2f}m -> soft warning at "
            f"{self.soft_geofence_radius:.2f}m, hard RTL at {self.geofence_radius:.2f}m."
        )

    def start_trajectory(
        self,
        trajectory_data: dict,
        mode: str = "LOCAL",
        trigger_epoch_ms: float = 0.0,
        execution: str = "onboard",
    ):
        """Start the show; the GCS-side watch/playback runs in a background task.

        Args:
            trajectory_data: The show. For onboard execution it must be the one
                last passed to upload_show().
            mode: "LOCAL" or "GLOBAL" — how streamed setpoints are expressed.
                Onboard missions are always flown in each drone's own pad
                frame, so this only affects execution="streamed".
            trigger_epoch_ms: UTC instant (ms) the show's t=0 should land on;
                0 or a past instant means "as soon as possible".
            execution: "onboard" (default, issue #47): every drone flies the
                mission upload_show() gave it, from its own storage and clock.
                The GCS stops offboard streaming, broadcasts one start trigger,
                and from then on transmits nothing per-drone — so losing the
                link mid-show does not trip the offboard-loss failsafe.
                "streamed": the legacy 10Hz unicast setpoint loop. Kept only
                because stock PX4 SITL has no onboard show executor; it does
                not scale over a real radio and is not for flying real shows.

        Raises:
            ValueError: On an unknown ``execution``, or an onboard start that
                has no verified upload of this show / no broadcast transport.
        """
        if execution not in ("onboard", "streamed"):
            raise ValueError(f"execution must be 'onboard' or 'streamed', got {execution!r}")
        if execution == "onboard":
            plan = self._onboard_show_plan(trajectory_data)

        self.stop_trajectory()
        self.active_trajectory = trajectory_data
        self._apply_geofence_envelope(trajectory_data)
        self.show_mode = mode
        self.show_started_at = time.time()
        if execution == "streamed":
            # Remap trajectory slots to currently healthy drones before playback
            plan = self.remap_trajectory_to_healthy_drones(trajectory_data)
        self.playing_trajectory = plan
        # Move everything alive now (imported modules, the fleet, this show)
        # out of the cyclic GC's reach. Otherwise a full collection rescans
        # that whole long-lived heap every ~1.4s of a show: ~11ms per pass at
        # 1,000 drones, which alone makes 50Hz physics ticks late (issue #51).
        # Reference counting still frees anything these objects stop using.
        gc.freeze()
        self.is_running_trajectory = True
        self.start_telemetry_recording()
        self.start_state_snapshotting()
        self.trajectory_task = asyncio.create_task(
            self._run_trajectory_loop(plan, mode, trigger_epoch_ms, execution)
        )

    def _onboard_show_plan(self, trajectory_data: dict) -> dict:
        """The per-drone trajectories an onboard start will fly, i.e. the
        staged assignment for drones holding a verified mission. Raises
        ValueError if an onboard start is not possible."""
        if self._broadcast_transmit is None:
            raise ValueError(
                "Onboard execution needs a broadcast radio and drone firmware with an onboard "
                f"show executor; the {self.backend} backend has neither (stock PX4 cannot fly a "
                "timed trajectory from storage). Use execution='streamed' for PX4 SITL development."
            )
        staged = self.staged_show
        if staged is None or compute_show_id(trajectory_data) != staged["show_id"]:
            raise ValueError(
                "This trajectory has not been uploaded to the fleet. Upload it "
                "(POST /api/upload-mission) before starting the show."
            )
        verified = [d for d, entry in staged["drones"].items() if entry["status"] == "VERIFIED"]
        if not verified:
            raise ValueError("No drone holds a verified mission for this show. Upload it again.")
        return {drone_id: staged["trajectory"][drone_id] for drone_id in verified}

    def _broadcast(self, payload: bytes) -> None:
        """One frame to every drone at once (target_system 0), never per-drone."""
        self._broadcast_transmit(payload, BROADCAST_SYSID)
        self.trigger_frames_sent += 1

    async def _trigger_onboard_show(self, trajectory_data: dict, trigger_epoch_ms: float) -> float:
        """Hand the fleet to its onboard executors and broadcast the start.

        Returns:
            The UTC instant (seconds) the show's t=0 was set to.
        """
        show_id = self.staged_show["show_id"]

        # Offboard is released first. MAVSDK's offboard plugin keeps re-sending
        # the last setpoint for as long as offboard is engaged, so leaving it on
        # would keep a unicast stream per drone on the air for the whole show —
        # and a dropout in it would still trip PX4's offboard-loss failsafe.
        async def _release(drone_id):
            try:
                await self.drones[drone_id].offboard.stop()
            except Exception as e:
                logging.debug(f"Drone {drone_id}: offboard.stop() before onboard show: {e}")

        await asyncio.gather(*(_release(d) for d in trajectory_data if d in self.drones))

        start_s = max(trigger_epoch_ms / 1000.0, time.time() + TRIGGER_LEAD_S)
        frame = encode_show_trigger(ShowCommand.START, show_id, int(start_s * 1_000_000))
        self._onboard_show_id = show_id
        print(f"Onboard show {show_id.hex()[:8]}: broadcasting start trigger for UTC {start_s:.3f}...")

        # The same frame, repeated until t=0. It carries an absolute start
        # instant, so a repeat is idempotent and reception jitter cannot skew
        # the start — repetition only buys reliability on a lossy radio.
        while True:
            self._broadcast(frame)
            remaining = start_s - time.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(TRIGGER_REPEAT_INTERVAL_S, remaining))
        return start_s

    def stop_trajectory(self):
        """Stop current trajectory playback."""
        if self._onboard_show_id is not None:
            # The drones are flying from their own storage, so cancelling the
            # GCS-side watch loop alone would leave them flying. One broadcast
            # stops them all; each holds where it is.
            try:
                self._broadcast(encode_show_trigger(ShowCommand.CANCEL, self._onboard_show_id))
            except Exception as e:
                logging.error(f"Failed to broadcast show CANCEL: {e}")
            self._onboard_show_id = None
        self.is_running_trajectory = False
        self.playing_trajectory = None
        self._playback_start_time = None
        self.stop_telemetry_recording()
        self.stop_state_snapshotting()
        # A deliberate stop ends the show; there is nothing left to offer resuming.
        discard_show_state(self.state_path)
        if self.trajectory_task:
            self.trajectory_task.cancel()
            self.trajectory_task = None
            print("Trajectory playback stopped.")

    # --- Telemetry Recording & CSV Export (issue #8) ---

    TELEMETRY_SAMPLE_INTERVAL_S = 0.5  # low-rate sampling, independent of the 10Hz control/safety loops

    def start_telemetry_recording(self):
        """
        Begin periodically sampling per-drone telemetry into self.telemetry_log for
        later CSV export. Safe to call multiple times: restarts recording from empty.
        """
        self.stop_telemetry_recording()
        self.telemetry_log = []
        self.is_recording_telemetry = True
        self._telemetry_recording_task = asyncio.create_task(self._telemetry_recording_loop())

    def stop_telemetry_recording(self):
        """Stop periodic telemetry sampling. Already-recorded samples are kept for export."""
        self.is_recording_telemetry = False
        if self._telemetry_recording_task:
            self._telemetry_recording_task.cancel()
            self._telemetry_recording_task = None

    async def _telemetry_recording_loop(self):
        """Samples every drone's telemetry at TELEMETRY_SAMPLE_INTERVAL_S for post-flight CSV export."""
        try:
            while True:
                sample_time = time.time()
                for drone_id, tel in self.telemetry.items():
                    self.telemetry_log.append({
                        "timestamp": sample_time,
                        "drone_id": drone_id,
                        "x": tel.get("fleet_x", 0.0),
                        "y": tel.get("fleet_y", 0.0),
                        "z": tel.get("fleet_z", 0.0),
                        "battery_pct": tel.get("battery_percent", 0.0),
                        "mode": tel.get("flight_mode", "UNKNOWN"),
                        "connected": tel.get("connected", False),
                    })
                await asyncio.sleep(self.TELEMETRY_SAMPLE_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def export_telemetry_csv(self, filepath: str) -> int:
        """
        Writes all recorded telemetry samples (see self.telemetry_log) to a CSV file
        at filepath. Returns the number of rows written.
        """
        fieldnames = ["timestamp", "drone_id", "x", "y", "z", "battery_pct", "mode", "connected"]
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.telemetry_log)
        return len(self.telemetry_log)

    # --- Show-state persistence & resume (issue #4) ---

    STATE_SNAPSHOT_INTERVAL_S = 5.0  # periodic, not every 10Hz control tick

    def start_state_snapshotting(self):
        """Begin periodically persisting the active trajectory + playback position to disk."""
        self.stop_state_snapshotting()
        self._state_snapshot_task = asyncio.create_task(self._state_snapshot_loop())

    def stop_state_snapshotting(self):
        """Stop periodic snapshotting. Does not touch any file already on disk."""
        if self._state_snapshot_task:
            self._state_snapshot_task.cancel()
            self._state_snapshot_task = None

    async def _state_snapshot_loop(self):
        """Writes a show-state snapshot to disk every STATE_SNAPSHOT_INTERVAL_S while a show runs."""
        try:
            while True:
                await asyncio.sleep(self.STATE_SNAPSHOT_INTERVAL_S)
                self._write_state_snapshot()
        except asyncio.CancelledError:
            pass

    def _write_state_snapshot(self):
        if not self.active_trajectory or self.show_started_at is None:
            return
        try:
            save_show_state(
                self.state_path,
                self.active_trajectory,
                self.show_mode,
                self.show_started_at,
                self.telemetry,
            )
        except Exception as e:
            logging.error(f"Failed to persist show state to {self.state_path}: {e}")

    def load_pending_resume_state(self) -> dict | None:
        """
        Called on GCS startup. If a saved show exists on disk, loads it into
        `self.pending_resume_state` (for an operator-facing resume/discard
        prompt) and returns it. Does NOT touch `active_trajectory`, arm, or
        launch anything — that only happens if the operator explicitly calls
        `resume_pending_show()`.
        """
        self.pending_resume_state = load_show_state(self.state_path)
        return self.pending_resume_state

    def resume_pending_show(self) -> float:
        """
        Applies a previously-loaded pending resume state: restores the active
        trajectory and calculates how far into it playback had reached, but
        does not start playback, arm, or launch anything — that still
        requires an explicit operator action (e.g. /api/start-trajectory).

        Returns the elapsed playback time (seconds) the show had reached
        when it was last saved, for the caller/frontend to display.

        Raises ValueError if there is no pending resume state, or if the saved
        trajectory no longer passes the upload gate — the snapshot is a plain
        file, so it is checked again exactly as an upload would be (issue
        #55). A refused show is left pending, for the operator to discard.
        """
        if not self.pending_resume_state:
            raise ValueError("No pending saved show state to resume.")

        state = self.pending_resume_state
        try:
            trajectory = gate_trajectory(state["trajectory"])
        except ValueError as e:
            raise ValueError(f"Saved show cannot be resumed: {e}") from e
        self.active_trajectory = trajectory
        self.show_mode = state["mode"]
        elapsed = max(0.0, state["saved_at"] - state["show_started_at"])
        self.pending_resume_state = None
        discard_show_state(self.state_path)
        return elapsed

    def discard_pending_show(self) -> None:
        """Discards a previously-loaded pending resume state without applying it."""
        self.pending_resume_state = None
        discard_show_state(self.state_path)

    async def _send_setpoint(self, drone_id: int, send) -> None:
        """Send one playback setpoint unless the drone was aborted since the tick began.

        The loop's own skip happens when a tick is built, but the sends then
        run as separate tasks, and an abort can land in between. On the sim
        backend a setpoint cancels an in-progress LAND/RTL outright, so the
        abort would silently undo itself; checking again here, with nothing
        awaited between the check and the send, closes that window.
        """
        if self._is_aborted(drone_id):
            return
        await send()

    async def _run_trajectory_loop(
        self,
        trajectory_data: dict,
        mode: str = "LOCAL",
        trigger_epoch_ms: float = 0.0,
        execution: str = "streamed",
    ):
        onboard = execution == "onboard"
        print(f"Starting trajectory playback loop in {mode} mode ({execution} execution)...")

        if onboard:
            # From here the GCS only watches: it publishes the planned point
            # for the HUD and notices the end of the show, but sends nothing.
            start_epoch_s = await self._trigger_onboard_show(trajectory_data, trigger_epoch_ms)
            # Align the loop's clock with the drones' UTC one, not with when
            # this line happened to run.
            start_time = asyncio.get_event_loop().time() - max(0.0, time.time() - start_epoch_s)
        else:
            # Clock synchronization (millisecond precision time-of-day trigger delay)
            if trigger_epoch_ms > 0:
                curr_epoch_s = time.time()
                trigger_s = trigger_epoch_ms / 1000.0
                wait_s = trigger_s - curr_epoch_s
                if wait_s > 0:
                    print(f"Clock Sync: Waiting {wait_s:.3f} seconds to trigger show at Epoch {trigger_s}...")
                    await asyncio.sleep(wait_s)

            start_time = asyncio.get_event_loop().time()
        
        self._playback_start_time = start_time
        # Calculate overall duration
        max_time = 0.0
        for drone_id, wps in trajectory_data.items():
            if wps:
                max_time = max(max_time, wps[-1]["time"])
                
        print(f"Trajectory duration: {max_time} seconds.")
        R = 6378137.0 # Earth radius for GPS projection

        # Bound before the try so the finally block below can always read it —
        # an unexpected exception (e.g. a drone dropping out of self.telemetry
        # mid-tick, or a MAVSDK error from the setpoint gather) skips the
        # except/else branches entirely, and an unbound name here would mask
        # the real exception with an UnboundLocalError.
        cancelled = False
        completed = False

        try:
            # 10Hz loop rate
            while self.is_running_trajectory:
                current_time = asyncio.get_event_loop().time()
                elapsed = current_time - start_time
                
                if elapsed > max_time + 1.0:
                    print("Trajectory complete. Hovering at last waypoints...")
                    self.is_running_trajectory = False
                    completed = True
                    break
                    
                # Calculate and send setpoints for each drone
                setpoint_tasks = []
                for drone_id, drone in self.drones.items():
                    if drone_id not in trajectory_data or not self.telemetry[drone_id]["connected"]:
                        continue
                    # Skip drones aborted out of the show (issue #59): their
                    # flight controller is commanding its own descent.
                    if self._is_aborted(drone_id):
                        continue
                        
                    wps = trajectory_data[drone_id]
                    # Calculate interpolated point
                    target = interpolate_waypoint(wps, elapsed)
                    
                    if target:
                        # Publish the commanded point alongside the measured one so
                        # clients can compute live deviation (|actual - planned|)
                        # without re-deriving the interpolation. Stored in the fleet
                        # frame and with z as altitude-positive-up, matching the
                        # fleet_x/y/z the position monitor writes — so the two are
                        # directly differenceable with no per-consumer conversion.
                        tel = self.telemetry[drone_id]
                        tel["planned_x"] = target["x"]
                        tel["planned_y"] = target["y"]
                        tel["planned_z"] = -target["z"]
                        tel["planned_valid"] = True

                        if onboard:
                            continue  # the drone is flying this itself

                        # Trajectory coordinates are expressed in the shared fleet frame
                        # (pad spacing baked in by the Studio generator). Subtract this
                        # drone's own pad offset to get back to its own local/home frame.
                        pad_x_off, pad_y_off = _pad_offset(drone_id)

                        if mode.upper() == "GLOBAL":
                            # GLOBAL MODE: Translate fleet-frame offsets using home GPS coordinates
                            home_lat = self.telemetry[drone_id]["home_lat"]
                            home_lon = self.telemetry[drone_id]["home_lon"]

                            if home_lat == 0.0 and home_lon == 0.0:
                                logging.error(
                                    f"Drone {drone_id}: home GPS not yet locked — skipping GLOBAL setpoint "
                                    f"this tick rather than using a fallback coordinate."
                                )
                                continue

                            rel_x = target["x"] - pad_x_off
                            rel_y = target["y"] - pad_y_off
                            dlat = rel_x / R
                            dlon = rel_y / (R * math.cos(math.radians(home_lat)))
                            lat_deg = home_lat + math.degrees(dlat)
                            lon_deg = home_lon + math.degrees(dlon)
                            alt_m = -target["z"] # convert NED Z down to altitude above home

                            setpoint_tasks.append(
                                self._send_setpoint(
                                    drone_id,
                                    functools.partial(
                                        drone.offboard.set_position_global,
                                        PositionGlobalYaw(
                                            lat_deg,
                                            lon_deg,
                                            alt_m,
                                            target["yaw"],
                                            PositionGlobalYaw.AltitudeType.REL_HOME,
                                        ),
                                    ),
                                )
                            )
                        else:
                            # LOCAL MODE: Send position, velocity, and acceleration NED (feed-forward)
                            # Convert fleet-frame trajectory coordinates to this drone's own
                            # local/home frame by subtracting its known pad offset (not the
                            # trajectory's first waypoint, which may not sit at the pad after
                            # a slot remap). Z has no pad offset (pads are flat), so it's used as-is.
                            local_x = target["x"] - pad_x_off
                            local_y = target["y"] - pad_y_off

                            pos = PositionNedYaw(local_x, local_y, target["z"], target["yaw"])
                            vel = VelocityNedYaw(target.get("vx", 0.0), target.get("vy", 0.0), target.get("vz", 0.0), target["yaw"])
                            acc = AccelerationNed(target.get("ax", 0.0), target.get("ay", 0.0), target.get("az", 0.0))
                            setpoint_tasks.append(
                                self._send_setpoint(
                                    drone_id,
                                    functools.partial(drone.offboard.set_position_velocity_acceleration_ned, pos, vel, acc),
                                )
                            )
                        
                if setpoint_tasks:
                    await self._for_each_drone(setpoint_tasks)
                    
                await asyncio.sleep(0.1)  # 10Hz
                
        except asyncio.CancelledError:
            # Cancellation comes from either stop_trajectory() (manual stop —
            # already discarded saved state itself before cancelling) or
            # shutdown() (GCS going away mid-show — deliberately leaves the
            # last snapshot on disk so a restart can offer to resume). Either
            # way, the finally block below must not discard state itself.
            print("Trajectory playback loop cancelled.")
            cancelled = True
        finally:
            self.is_running_trajectory = False
            # Once playback stops there is no commanded point any more, so clear
            # the flag rather than leaving the last setpoint published — a stale
            # plan would keep the HUD scoring deviation against a frozen target
            # as the fleet lands or returns home.
            for tel in self.telemetry.values():
                tel["planned_valid"] = False
            # A finished (or abandoned-by-shutdown) onboard show needs no
            # CANCEL: drones hold their last waypoint on their own.
            if onboard:
                self._onboard_show_id = None
            # Natural completion doesn't go through stop_trajectory(), so stop
            # recording here too (stop_trajectory() already covers the manual-stop path).
            self.stop_telemetry_recording()
            if not cancelled:
                # Not on cancel: stop_trajectory() already cleared it, and a
                # start_trajectory() right after may have set it for a new show.
                self._playback_start_time = None
                self.stop_state_snapshotting()
                discard_show_state(self.state_path)
                # Cancellation means stop_trajectory() or an emergency, which
                # set the lifecycle themselves. Only an end the loop reached on
                # its own is the loop's to report.
                if completed:
                    self.fleet_fsm.advance(
                        LifecycleState.RUNNING, LifecycleState.SHOW_COMPLETE, trigger="trajectory_complete"
                    )
                else:
                    self.fleet_fsm.advance(
                        LifecycleState.RUNNING, LifecycleState.FORMATION_HOLD, trigger="trajectory_error"
                    )

    def verify_launch_geometry(self, trajectory_data: dict, tolerance_m: float = 0.5) -> dict:
        """
        Verifies that each connected drone is physically placed within the tolerance_m
        envelope of its expected starting position (first waypoint) before arming/takeoff.
        """
        results = {}
        all_passed = True
        
        # Remap first to match actual drone slots
        remapped = self.remap_trajectory_to_healthy_drones(trajectory_data)
        
        for drone_id, wps in remapped.items():
            if not wps:
                continue
            
            # First waypoint at t=0
            first_wp = wps[0]
            
            if drone_id not in self.telemetry or not self.telemetry[drone_id]["connected"]:
                results[str(drone_id)] = {"status": "DISCONNECTED", "msg": "Drone not connected."}
                all_passed = False
                continue
                
            tel = self.telemetry[drone_id]
            # Expected first-waypoint position, in the shared fleet frame.
            expected_x = first_wp["x"]
            expected_y = first_wp["y"]
            expected_z = -first_wp["z"] # convert NED down to height

            # Actual position, converted from this drone's own local/home frame
            # into the same fleet frame (see LAUNCH_PAD_SPACING_Y_M).
            curr_x = tel["fleet_x"]
            curr_y = tel["fleet_y"]
            curr_z = tel["fleet_z"]

            dist = ((curr_x - expected_x)**2 + (curr_y - expected_y)**2 + (curr_z - expected_z)**2)**0.5
            
            passed = dist <= tolerance_m
            if not passed:
                all_passed = False
                
            results[str(drone_id)] = {
                "status": "PASSED" if passed else "FAILED",
                "current_pos": [curr_x, curr_y, curr_z],
                "expected_pos": [expected_x, expected_y, expected_z],
                "distance_error_m": round(dist, 2),
                "msg": f"Placed at ({curr_x:.2f}, {curr_y:.2f}, {curr_z:.2f}) - expected ({expected_x:.2f}, {expected_y:.2f}, {expected_z:.2f}). Error: {dist:.2f}m"
            }
            
        return {
            "all_passed": all_passed,
            "drones": results
        }

    async def _proximity_monitor_loop(self, safety_limit: float = DEFAULT_LIMITS.min_separation_m):
        """
        Monitors spatial separation between all active/armed drones at 10Hz.
        Triggers emergency failsafe (RTL) if distance is breached.
        """
        logging.info("Proximity safety monitor active.")
        try:
            while True:
                # Find connected & armed drones
                active_ids = [
                    d_id for d_id, tel in self.telemetry.items()
                    if tel["connected"] and tel["armed"]
                ]
                
                if len(active_ids) < 2:
                    self.proximity_breaches.clear()
                else:
                    current_breaches = set()
                    new_breach_found = False
                    # Check distances between all pairs, in the shared fleet frame
                    # (consistent with verify_launch_geometry and the Studio-exported
                    # trajectory coordinates). Uses a cKDTree instead of an O(n^2)
                    # nested loop — same approach as taramandal-studio's validator.py,
                    # which stays fast as fleet size grows toward hundreds of drones.
                    coords = [
                        [self.telemetry[d_id]["fleet_x"], self.telemetry[d_id]["fleet_y"], self.telemetry[d_id]["fleet_z"]]
                        for d_id in active_ids
                    ]
                    tree = cKDTree(coords)
                    for i, j in tree.query_pairs(safety_limit):
                        id1, id2 = active_ids[i], active_ids[j]
                        pair = (min(id1, id2), max(id1, id2))
                        current_breaches.add(pair)
                        if not self.proximity_breaches.get(pair, False):
                            self.proximity_breaches[pair] = True
                            new_breach_found = True
                            dist = ((coords[i][0] - coords[j][0])**2 +
                                    (coords[i][1] - coords[j][1])**2 +
                                    (coords[i][2] - coords[j][2])**2)**0.5
                            logging.warning(f"🚨 PROXIMITY FAILSAFE: Drone {id1} and Drone {id2} are too close ({dist:.2f}m)!")

                    # Clear breach tracking for any pair that is no longer violating limits
                    for pair in list(self.proximity_breaches.keys()):
                        if self.proximity_breaches[pair] and pair not in current_breaches:
                            self.proximity_breaches[pair] = False
                            logging.info(f"Proximity breach between Drone {pair[0]} and Drone {pair[1]} cleared.")

                    # Trigger RTL once per tick (not inline per-pair), after the full scan completes.
                    if new_breach_found:
                        logging.warning("🚨 TRIGGERING EMERGENCY RTL FOR ALL VEHICLES.")
                        await self.trigger_rtl(trigger="failsafe:proximity")
                                
                await asyncio.sleep(0.1)  # 10Hz
        except asyncio.CancelledError:
            logging.info("Proximity safety monitor stopped.")

    async def _geofence_monitor_loop(self):
        """
        Monitors each armed drone's distance from home (0, 0) at 10Hz against
        two thresholds derived from the same trajectory envelope (issue #53,
        set by `_apply_geofence_envelope`):

          * `soft_geofence_radius` — logs a warning once per breach, does NOT
            trigger RTL. Skipped entirely while None (no show loaded yet).
          * `geofence_radius` — hard boundary; triggers RTL once per breach,
            same as before.

        Breach/warning tracking resets independently, per drone, when it
        clears back inside each threshold (not tied to disarming).
        """
        logging.info(
            f"Geofence monitor active (soft warning: {self.soft_geofence_radius}m, "
            f"hard RTL: {self.geofence_radius}m)."
        )
        try:
            while True:
                for drone_id, tel in self.telemetry.items():
                    if not tel["connected"] or not tel["armed"]:
                        continue

                    x = tel["local_x"]
                    y = tel["local_y"]
                    z = tel["local_z"]

                    dist = (x**2 + y**2) ** 0.5           # horizontal distance from home
                    dist3d = (x**2 + y**2 + z**2) ** 0.5  # total 3D distance from home

                    already_breached = self.geofence_breaches.get(drone_id, False)
                    already_warned = self.geofence_warnings.get(drone_id, False)

                    if dist > self.geofence_radius:
                        if not already_breached:
                            logging.warning(
                                f"🚧 GEOFENCE BREACH: Drone {drone_id} is {dist:.2f}m from home "
                                f"(3D: {dist3d:.2f}m) — limit is {self.geofence_radius}m. "
                                f"Triggering RTL."
                            )
                            self.geofence_breaches[drone_id] = True
                            await self.trigger_rtl(trigger="failsafe:geofence")
                    elif already_breached:
                        # Within bounds — clear breach flag if previously set
                        self.geofence_breaches[drone_id] = False

                    if self.soft_geofence_radius is not None and dist > self.soft_geofence_radius:
                        if not already_warned:
                            logging.warning(
                                f"⚠️ GEOFENCE SOFT WARNING: Drone {drone_id} is {dist:.2f}m from home "
                                f"(3D: {dist3d:.2f}m) — soft warning threshold is "
                                f"{self.soft_geofence_radius}m."
                            )
                            self.geofence_warnings[drone_id] = True
                    elif already_warned:
                        self.geofence_warnings[drone_id] = False

                await asyncio.sleep(0.1)  # 10Hz
        except asyncio.CancelledError:
            logging.info("Geofence monitor stopped.")

    #: Lifecycle monitor rate. Transitions are operator-facing (HUD, gating),
    #: so 5Hz is ample; the 10Hz safety loops above don't depend on it.
    LIFECYCLE_POLL_S = 0.2
    #: How long after a launch task ends the monitor waits for a drone to show up
    #: as armed before it calls the launch failed. arm() returning and the armed
    #: flag reaching telemetry are two different moments (the flag comes from the
    #: autopilot's own stream), so judging at the instant the task ends can
    #: report "no drone armed" for a drone that did arm, and send the fleet back
    #: to STAGED while it is armed. Matches the heartbeat staleness bar: if
    #: nothing has armed within that long, the launch really did fail.
    LAUNCH_ARM_GRACE_S = 2.5
    #: Fleet altitude, metres, above which an armed drone counts as airborne.
    TAKEOFF_DETECT_ALT_M = 0.5
    #: How far below its hover altitude a drone may be and still count as in formation.
    FORMATION_ALT_TOLERANCE_M = 0.5

    async def _lifecycle_monitor_loop(self):
        """Advances the fleet lifecycle from telemetry at LIFECYCLE_POLL_S."""
        logging.info("Fleet lifecycle monitor active.")
        try:
            while True:
                try:
                    self.evaluate_lifecycle()
                except Exception as e:
                    # A bad telemetry entry must not kill the monitor for the
                    # rest of the process's life.
                    logging.error(f"Fleet lifecycle monitor error: {e}")
                await asyncio.sleep(self.LIFECYCLE_POLL_S)
        except asyncio.CancelledError:
            logging.info("Fleet lifecycle monitor stopped.")

    def evaluate_lifecycle(self) -> None:
        """Make at most one observed transition, based on current telemetry.

        Fleet-level phases are defined by the leading drone going up (the first
        to arm moves PREARM -> ARMED, the first to climb moves ARMED -> TAKEOFF)
        and by the whole armed fleet for "done" (formation altitude reached,
        everyone disarmed). A drone that never armed isn't part of the flight,
        so it doesn't hold the fleet in TAKEOFF.
        """
        fsm = self.fleet_fsm
        state = fsm.state
        launching = self._launch_in_progress()
        # Last-known armed flag, connected or not: a link loss must not read as
        # "the fleet has disarmed".
        armed_ids = [d_id for d_id, tel in self.telemetry.items() if tel.get("armed")]

        if state in AIRBORNE_STATES and not armed_ids and not launching:
            if state is LifecycleState.RUNNING:
                self.stop_trajectory()
            fsm.transition(LifecycleState.DISARMED, operator=SYSTEM_OPERATOR, trigger="telemetry:all_disarmed")
            return

        if state is LifecycleState.PREARM:
            if armed_ids:
                fsm.advance(state, LifecycleState.ARMED, trigger="telemetry:first_drone_armed")
            elif not launching:
                ended = self._launch_finished_at
                if ended is None or time.monotonic() - ended >= self.LAUNCH_ARM_GRACE_S:
                    fsm.advance(state, LifecycleState.STAGED, trigger="launch_failed:no_drone_armed")
        elif state is LifecycleState.ARMED:
            if any(self.telemetry[d_id].get("fleet_z", 0.0) >= self.TAKEOFF_DETECT_ALT_M for d_id in armed_ids):
                fsm.advance(state, LifecycleState.TAKEOFF, trigger="telemetry:climbing")
        elif state is LifecycleState.TAKEOFF:
            at_altitude = all(
                self.telemetry[d_id].get("fleet_z", 0.0)
                >= _hover_altitude(d_id) - self.FORMATION_ALT_TOLERANCE_M
                for d_id in armed_ids
            )
            if at_altitude and not launching:
                fsm.advance(state, LifecycleState.FORMATION_HOLD, trigger="telemetry:formation_altitude_reached")

    def remap_trajectory_to_healthy_drones(self, trajectory_data: dict) -> dict:
        """
        Re-assigns trajectory slots to currently healthy drones.

        A drone is healthy if it is:
          - connected
          - battery_percent > 20%
          - satellites >= 6

        Healthy drones are sorted by their physical drone_id (index).
        Trajectory slots (0, 1, 2, ...) are assigned in order to healthy drones.
        Extra trajectory slots beyond available healthy drones are dropped.
        Extra healthy drones beyond available slots are left idle.

        Returns a new dict keyed by actual drone IDs mapped to their remapped waypoints.
        """
        # Determine healthy drones sorted by physical index
        healthy_ids = sorted(
            [
                d_id
                for d_id, tel in self.telemetry.items()
                if tel["connected"]
                and tel["battery_percent"] > 20.0
                and tel["satellites"] >= 6
                and not tel.get("aborted")
            ]
        )

        # Trajectory slots are integer keys (0, 1, 2, ...)
        slot_keys = sorted([k for k in trajectory_data.keys() if isinstance(k, int)])

        skipped_drones = [d_id for d_id in self.telemetry if d_id not in healthy_ids]
        if skipped_drones:
            logging.warning(
                f"Swarm re-mapper: Skipping unhealthy/disconnected drones: {skipped_drones}"
            )

        remapped: dict = {}
        for slot_index, slot_key in enumerate(slot_keys):
            if slot_index >= len(healthy_ids):
                logging.warning(
                    f"Swarm re-mapper: No healthy drone available for trajectory slot {slot_key} — dropping slot."
                )
                break
            actual_drone_id = healthy_ids[slot_index]
            remapped[actual_drone_id] = trajectory_data[slot_key]
            logging.info(
                f"Swarm re-mapper: Trajectory slot {slot_key} → Drone {actual_drone_id}"
            )

        idle_drones = healthy_ids[len(slot_keys):]
        if idle_drones:
            logging.info(
                f"Swarm re-mapper: Drones {idle_drones} have no trajectory slot — will remain idle."
            )

        return remapped
