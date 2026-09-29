"""Vectorized physics for every drone in the fleet.

The one design constraint that makes 100+ drones possible: **one asyncio task,
one numpy tick, for the whole fleet** — not one task per drone. The real
`tara_gcs.drone_manager.DroneManager` spawns 8 asyncio tasks per drone plus a
`mavsdk_server` gRPC process per drone; at 100 drones that's 800 tasks and 100
processes. Here, 1000 drones cost the same handful of array operations per
tick as 10 do.

Every drone's position lives in **its own local frame**, origin at its own
launch pad — exactly like a real PX4 SITL instance's NED origin. Nothing here
knows about the shared "fleet frame" (pad grid, altitude layering); that
conversion is `tara_gcs.drone_manager._pad_offset`'s job, unchanged, on either
backend. That is what lets the exact same trajectory-playback code drive this
simulator as drives real MAVSDK.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

import numpy as np

#: Real PX4/SITL commonly settles EKF/health checks in a few hundred ms in
#: simulation. This is not modeled physically — it exists so a health-check
#: loop that waits for "armable" behaves like a real one instead of returning
#: instantly, without meaningfully slowing down a 100-drone launch.
DEFAULT_ARMABLE_DELAY_S = 0.3

#: Metres per second the descent ramps at during LAND/RTL's final phase.
DEFAULT_DESCENT_RATE_MS = 1.0

#: Horizontal distance from home, in metres, at which RTL's "fly home" phase
#: is considered complete and it switches to descending.
RTL_ARRIVAL_RADIUS_M = 0.3

#: Altitude, in metres, below which a descending drone is considered landed.
LANDED_ALTITUDE_M = 0.05

#: PX4's COM_OF_LOSS_T default: how long a drone in offboard mode tolerates
#: hearing no setpoint before it gives up on the GCS and fails safe.
DEFAULT_OFFBOARD_LOSS_TIMEOUT_S = 1.0

#: How far behind its schedule the tick loop may fall before it gives up on
#: catching up and re-anchors to now. Up to this much backlog is worked off by
#: ticking without sleeping; beyond it, back-to-back catch-up ticks would only
#: starve every other task on the loop, so the backlog is dropped instead.
MAX_TICK_BACKLOG_S = 0.1


@dataclass
class SimConfig:
    """Tunable physics/realism parameters for one :class:`FleetState`.

    Attributes:
        tick_hz: Physics update rate.
        max_velocity_ms: 3D speed cap, m/s.
        max_accel_ms2: 3D acceleration cap, m/s^2 (includes feedforward + wind).
        kp: Position-error gain of the setpoint tracker.
        kd: Velocity-error gain of the setpoint tracker.
        setpoint_latency_s: Delay between a setpoint being commanded and the
            tracker starting to pursue it — models command/telemetry round trip.
        position_noise_std_m: Std-dev of Gaussian noise added to the position
            *readout* only (not the physics state), so safety-check math sees
            realistic sensor uncertainty. ~0.02 for RTK, ~1.0 for plain GPS.
        wind_std_ms2: Std-dev of a per-tick random acceleration disturbance.
        battery_drain_per_s: Percent drained per second while armed.
        dropout_prob_per_tick: Probability per tick that a drone's `connected`
            flag flips, to exercise failsafe/health-gated code paths. 0 by
            default so deterministic tests are actually deterministic.
        armable_delay_s: Simulated EKF/health settle time after connect.
        descent_rate_ms: Vertical speed during the LAND/RTL descent phase.
        offboard_loss_timeout_s: PX4's COM_OF_LOSS_T. A drone in offboard
            mode that stops receiving setpoints for this long fails safe to
            LAND (PX4's COM_OBL_ACT default with no RC). Setpoints are
            considered received whenever offboard is active and the link is
            up, modelling MAVSDK's offboard plugin re-sending the last
            setpoint for as long as offboard is engaged — so this only fires
            on a genuine link loss, never on a GCS that is merely quiet.
        rotor_tau_s: First-order time constant, seconds, modelling how fast a
            real rotor's thrust (hence acceleration) can actually change —
            50-150 ms is typical, so 0.08 is a representative default (issue
            #60). The PD tracker's ``accel`` output above is a *commanded*
            acceleration; a real airframe cannot instantly deliver it, so the
            acceleration actually applied to ``velocity`` lags the command by
            this time constant. ``0.0`` (the default) disables the filter
            entirely — commanded acceleration is applied immediately, exactly
            the pre-#60 behaviour — so every existing test and caller that
            doesn't opt in is unaffected.
        rng_seed: Seed for the noise/wind/dropout generator. ``None`` for
            nondeterministic (real) runs.
    """

    tick_hz: float = 50.0
    max_velocity_ms: float = 8.0
    max_accel_ms2: float = 15.0
    kp: float = 4.0
    kd: float = 3.0
    setpoint_latency_s: float = 0.05
    position_noise_std_m: float = 0.02
    wind_std_ms2: float = 0.05
    battery_drain_per_s: float = 100.0 / 600.0
    dropout_prob_per_tick: float = 0.0
    armable_delay_s: float = DEFAULT_ARMABLE_DELAY_S
    descent_rate_ms: float = DEFAULT_DESCENT_RATE_MS
    offboard_loss_timeout_s: float = DEFAULT_OFFBOARD_LOSS_TIMEOUT_S
    rotor_tau_s: float = 0.0
    rng_seed: int | None = None

    @property
    def dt(self) -> float:
        return 1.0 / self.tick_hz


class FleetState:
    """Shared vectorized state and physics for ``n`` simulated drones.

    Position/velocity/target arrays are ``(n, 3)`` in each drone's own local
    NED-derived frame, stored as ``(north_m, east_m, altitude_m)`` — altitude
    positive-up, so it matches what `drone_manager._monitor_position` computes
    from real telemetry (`-pos.position.down_m`) without a sign flip needed at
    the read site; the `down_m` sign flip happens only at the
    :class:`~tara_sim.system.SimSystem` boundary, matching where the real
    MAVSDK object would also need it.
    """

    def __init__(self, n: int, config: SimConfig | None = None):
        if n < 0:
            raise ValueError(f"n must be non-negative, got {n}")
        self.n = n
        self.config = config or SimConfig()
        self._rng = np.random.default_rng(self.config.rng_seed)

        self.position = np.zeros((n, 3))
        self.velocity = np.zeros((n, 3))
        self.yaw_deg = np.zeros(n)

        # Acceleration actually applied last tick, after the rotor-lag filter
        # (see SimConfig.rotor_tau_s) — the filter's own state, since a
        # first-order lag needs to remember what it was converging from.
        self._applied_accel = np.zeros((n, 3))

        # What the controller is actually tracking right now.
        self.target_position = np.zeros((n, 3))
        self.target_velocity_ff = np.zeros((n, 3))
        self.target_accel_ff = np.zeros((n, 3))
        self.target_yaw_deg = np.zeros(n)

        # Newly-commanded setpoints, applied after config.setpoint_latency_s —
        # a single pending slot per drone, since offboard setpoints are a
        # streamed register (each new one supersedes the last), not a queue.
        self._staged_position = np.zeros((n, 3))
        self._staged_velocity_ff = np.zeros((n, 3))
        self._staged_accel_ff = np.zeros((n, 3))
        self._staged_yaw_deg = np.zeros(n)
        self._staged_at = np.full(n, -np.inf)
        self._staged_pending = np.zeros(n, dtype=bool)

        self.armed = np.zeros(n, dtype=bool)
        self.connected = np.ones(n, dtype=bool)
        self.offboard_active = np.zeros(n, dtype=bool)
        self.battery_pct = np.full(n, 100.0)
        self.satellites = np.full(n, 16, dtype=int)

        self.landing = np.zeros(n, dtype=bool)
        self.rtl_pending = np.zeros(n, dtype=bool)  # "flying home" phase of RTL
        self._connected_since = np.zeros(n)

        # Offboard link-loss failsafe (see SimConfig.offboard_loss_timeout_s).
        self._last_setpoint_rx = np.full(n, -np.inf)
        self.offboard_failsafe = np.zeros(n, dtype=bool)
        #: Unicast offboard setpoints each drone has received from the GCS —
        #: what a test counts to prove a show flew without any.
        self.setpoints_received = np.zeros(n, dtype=int)

        # Onboard storage and the onboard mission executor: what lets a drone
        # fly a show with no GCS in the loop. `storage` stands in for the SD
        # card MAVLink FTP writes to; a loaded mission is held as arrays in
        # the drone's own local frame (altitude-up, like `position`).
        self.storage: list[dict[str, bytes]] = [{} for _ in range(n)]
        self._mission: list[tuple[np.ndarray, np.ndarray, np.ndarray] | None] = [None] * n
        self.mission_show_id: list[bytes | None] = [None] * n
        self.mission_start_utc = np.full(n, np.nan)
        self.mission_active = np.zeros(n, dtype=bool)
        self.mission_complete = np.zeros(n, dtype=bool)
        #: The drone's GNSS-disciplined UTC clock, in seconds. A mission's
        #: timeline runs off this, not `time_s` — a real fleet shares UTC, not
        #: a simulator tick count. Replaceable so tests can pin it.
        self.utc_clock = time.time

        self.flight_mode = ["MANUAL"] * n
        self.gps_fix_type = ["FIX_3D"] * n

        self.time_s = 0.0
        self._running = False
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> asyncio.Task:
        """Start the background tick task. Idempotent."""
        if self._task is None or self._task.done():
            self._running = True
            self._task = asyncio.create_task(self._run())
        return self._task

    async def _run(self) -> None:
        """Tick every ``dt`` against fixed deadlines, not ``sleep(dt)`` after each tick.

        ``tick(dt)`` always advances simulated time by exactly ``dt``, so the
        loop's period has to average exactly ``dt`` in wall-clock time too, or
        the fleet flies in slow motion. Sleeping a full ``dt`` after every tick
        adds the tick's own cost and the scheduler's wake-up delay to every
        period, which caps real-time factor below 1.0 even on an idle machine.
        On Windows, where a 20ms sleep rounds up to ~31ms, it was 0.64. Sleeping
        until the next deadline absorbs both.
        """
        dt = self.config.dt
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        try:
            while self._running:
                self.tick(dt)
                next_tick += dt
                delay = next_tick - loop.time()
                if delay < -MAX_TICK_BACKLOG_S:
                    next_tick = loop.time()
                    delay = 0.0
                await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()

    # ----------------------------------------------------------- commands

    def command_setpoint(
        self,
        drone_id: int,
        position: tuple[float, float, float],
        velocity_ff: tuple[float, float, float] = (0.0, 0.0, 0.0),
        accel_ff: tuple[float, float, float] = (0.0, 0.0, 0.0),
        yaw_deg: float = 0.0,
    ) -> None:
        """Stage a new setpoint; takes effect after ``setpoint_latency_s``."""
        self._staged_position[drone_id] = position
        self._staged_velocity_ff[drone_id] = velocity_ff
        self._staged_accel_ff[drone_id] = accel_ff
        self._staged_yaw_deg[drone_id] = yaw_deg
        self._staged_at[drone_id] = self.time_s
        self._staged_pending[drone_id] = True
        self._last_setpoint_rx[drone_id] = self.time_s
        self.setpoints_received[drone_id] += 1
        self.landing[drone_id] = False
        self.rtl_pending[drone_id] = False

    def command_land(self, drone_id: int) -> None:
        """Descend straight down from the current position, then disarm."""
        self.mission_active[drone_id] = False
        self.rtl_pending[drone_id] = False
        self.landing[drone_id] = True

    def command_rtl(self, drone_id: int) -> None:
        """Fly to (0, 0) at the current altitude, then land."""
        self.mission_active[drone_id] = False
        self.landing[drone_id] = False
        self.rtl_pending[drone_id] = True

    def command_arm(self, drone_id: int) -> None:
        self.armed[drone_id] = True

    def command_disarm(self, drone_id: int) -> None:
        self.armed[drone_id] = False
        self.offboard_active[drone_id] = False
        self.mission_active[drone_id] = False
        self.landing[drone_id] = False
        self.rtl_pending[drone_id] = False

    def command_offboard_start(self, drone_id: int) -> None:
        """Engage offboard mode. Takes over from any onboard mission."""
        self.offboard_active[drone_id] = True
        self._last_setpoint_rx[drone_id] = self.time_s
        self.mission_active[drone_id] = False

    # ------------------------------------------------------ onboard mission

    def load_mission(self, drone_id: int, show_id: bytes, waypoints: list[dict]) -> None:
        """Load a decoded mission (drone-local NED waypoints) ready to fly.

        Args:
            drone_id: Which drone.
            show_id: The show the mission belongs to, matched against triggers.
            waypoints: As returned by ``tara_core.decode_mission`` — NED, so z
                is flipped to altitude-up here to match this class's frame.
        """
        times = np.array([wp["time"] for wp in waypoints], dtype=float)
        positions = np.array([(wp["x"], wp["y"], -wp["z"]) for wp in waypoints], dtype=float)
        yaws = np.array([wp.get("yaw", 0.0) for wp in waypoints], dtype=float)
        self._mission[drone_id] = (times, positions, yaws)
        self.mission_show_id[drone_id] = show_id
        self.mission_active[drone_id] = False
        self.mission_complete[drone_id] = False

    def start_mission(self, drone_id: int, start_utc_s: float) -> bool:
        """Arm the loaded mission to begin at ``start_utc_s``. Returns whether it did.

        Refused (False) when nothing is loaded or the drone is not armed — a
        grounded drone has nothing to fly the show with.
        """
        if self._mission[drone_id] is None or not self.armed[drone_id]:
            return False
        self.mission_start_utc[drone_id] = start_utc_s
        self.mission_active[drone_id] = True
        self.mission_complete[drone_id] = False
        return True

    def cancel_mission(self, drone_id: int) -> None:
        """Abandon the mission and hold wherever the drone is right now."""
        if not self.mission_active[drone_id]:
            return
        self.mission_active[drone_id] = False
        self.target_position[drone_id] = self.position[drone_id]
        self.target_velocity_ff[drone_id] = 0.0
        self.target_accel_ff[drone_id] = 0.0

    def command_kill(self, drone_id: int) -> None:
        """Cut the motors: disarm immediately, wherever the drone is.

        There is no free-fall model (a disarmed drone's velocity is simply
        zeroed each tick), so a killed drone is put on the ground straight
        away rather than left frozen in mid-air, reporting a stale altitude
        while disarmed.
        """
        self.command_disarm(drone_id)
        self.position[drone_id, 2] = 0.0
        self.velocity[drone_id] = 0.0
        self.flight_mode[drone_id] = "TERMINATED"

    def is_armable(self, drone_id: int) -> bool:
        return (self.time_s - self._connected_since[drone_id]) >= self.config.armable_delay_s

    # --------------------------------------------------------------- tick

    def tick(self, dt: float) -> None:
        """Advance the whole fleet's physics by ``dt`` seconds."""
        self.time_s += dt
        cfg = self.config

        due = self._staged_pending & ((self.time_s - self._staged_at) >= cfg.setpoint_latency_s)
        if np.any(due):
            self.target_position[due] = self._staged_position[due]
            self.target_velocity_ff[due] = self._staged_velocity_ff[due]
            self.target_accel_ff[due] = self._staged_accel_ff[due]
            self.target_yaw_deg[due] = self._staged_yaw_deg[due]
            self._staged_pending[due] = False

        self._apply_offboard_loss_failsafe()
        self._apply_mission()
        self._apply_landing_and_rtl()

        pos_err = self.target_position - self.position
        vel_err = self.target_velocity_ff - self.velocity
        accel = cfg.kp * pos_err + cfg.kd * vel_err + self.target_accel_ff
        if cfg.wind_std_ms2 > 0:
            accel = accel + self._rng.normal(0.0, cfg.wind_std_ms2, size=(self.n, 3))
        accel = _clamp_norm(accel, cfg.max_accel_ms2)

        if cfg.rotor_tau_s > 0:
            # Exact discrete update of a continuous first-order lag
            # (d(applied)/dt = (commanded - applied) / tau): applied converges
            # on commanded with time constant tau regardless of dt, rather
            # than the dt/tau approximation drifting at low tick rates.
            alpha = 1.0 - math.exp(-dt / cfg.rotor_tau_s)
            self._applied_accel += (accel - self._applied_accel) * alpha
            accel = self._applied_accel
        else:
            self._applied_accel = accel

        active = self.armed
        self.velocity[active] += accel[active] * dt
        self.velocity[~active] = 0.0
        self.velocity[:] = _clamp_norm(self.velocity, cfg.max_velocity_ms)
        self.position[active] += self.velocity[active] * dt
        # Ground is a hard floor regardless of state — a controller overshoot
        # (or any future state this doesn't anticipate) must not tunnel through it.
        below_ground = self.position[:, 2] < 0.0
        self.position[below_ground, 2] = 0.0
        self.velocity[below_ground & (self.velocity[:, 2] < 0.0), 2] = 0.0

        yaw_err = ((self.target_yaw_deg - self.yaw_deg + 180.0) % 360.0) - 180.0
        self.yaw_deg = (self.yaw_deg + np.clip(yaw_err, -720.0 * dt, 720.0 * dt)) % 360.0

        self.battery_pct[active] = np.clip(self.battery_pct[active] - cfg.battery_drain_per_s * dt, 0.0, 100.0)

        if cfg.dropout_prob_per_tick > 0:
            flips = self._rng.random(self.n) < cfg.dropout_prob_per_tick
            self.connected[flips] = ~self.connected[flips]

        landed = active & self.landing & (self.position[:, 2] <= LANDED_ALTITUDE_M) & (np.abs(self.velocity[:, 2]) < 0.1)
        for drone_id in np.nonzero(landed)[0]:
            self.command_disarm(int(drone_id))
            self.flight_mode[int(drone_id)] = "LANDED"

    def _apply_offboard_loss_failsafe(self) -> None:
        cfg = self.config
        # MAVSDK keeps re-sending the last setpoint while offboard is engaged,
        # so an offboard drone hears one every tick its link is up.
        hearing = self.offboard_active & self.connected
        self._last_setpoint_rx[hearing] = self.time_s

        lost = (
            self.armed
            & self.offboard_active
            & ~self.landing
            & ~self.rtl_pending
            & ((self.time_s - self._last_setpoint_rx) > cfg.offboard_loss_timeout_s)
        )
        for i in np.nonzero(lost)[0]:
            i = int(i)
            self.offboard_active[i] = False
            self.offboard_failsafe[i] = True
            self.command_land(i)

    def _apply_mission(self) -> None:
        """Drive every drone flying an onboard mission straight from its own
        stored trajectory — no staging latency, because nothing crosses a link."""
        flying = self.mission_active & self.armed
        if not np.any(flying):
            return
        now = self.utc_clock()
        for i in np.nonzero(flying)[0]:
            i = int(i)
            elapsed = now - self.mission_start_utc[i]
            if elapsed < 0.0:
                continue  # trigger received, t=0 not yet reached: keep holding
            times, positions, yaws = self._mission[i]
            self._staged_pending[i] = False

            if elapsed >= times[-1]:
                self.target_position[i] = positions[-1]
                self.target_velocity_ff[i] = 0.0
                self.target_accel_ff[i] = 0.0
                self.target_yaw_deg[i] = yaws[-1]
                self.mission_active[i] = False
                self.mission_complete[i] = True
                continue

            k = int(np.searchsorted(times, elapsed, side="right"))
            if k == 0:
                self.target_position[i] = positions[0]
                self.target_velocity_ff[i] = 0.0
                self.target_accel_ff[i] = 0.0
                self.target_yaw_deg[i] = yaws[0]
                continue
            t0, t1 = times[k - 1], times[k]
            span = t1 - t0
            ratio = (elapsed - t0) / span if span > 0 else 0.0
            p0, p1 = positions[k - 1], positions[k]
            self.target_position[i] = p0 + ratio * (p1 - p0)
            self.target_velocity_ff[i] = (p1 - p0) / span if span > 0 else 0.0
            # Shorter-arc yaw, matching tara_core.interpolate_waypoint.
            diff = (yaws[k] - yaws[k - 1]) % 360.0
            if diff > 180.0:
                diff -= 360.0
            self.target_yaw_deg[i] = (yaws[k - 1] + ratio * diff) % 360.0
            self.target_accel_ff[i] = 0.0

    def _apply_landing_and_rtl(self) -> None:
        cfg = self.config
        rtl_ids = np.nonzero(self.rtl_pending)[0]
        for i in rtl_ids:
            i = int(i)
            horiz = float(np.hypot(self.position[i, 0], self.position[i, 1]))
            if horiz <= RTL_ARRIVAL_RADIUS_M:
                self.rtl_pending[i] = False
                self.landing[i] = True
            else:
                self.target_position[i, 0] = 0.0
                self.target_position[i, 1] = 0.0
                self.target_velocity_ff[i] = 0.0
                self.target_accel_ff[i] = 0.0
                self.flight_mode[i] = "RETURN_TO_LAUNCH"

        land_ids = np.nonzero(self.landing)[0]
        for i in land_ids:
            i = int(i)
            self.target_position[i, 0] = self.position[i, 0]
            self.target_position[i, 1] = self.position[i, 1]
            # A constant downward velocity feedforward would never taper to
            # zero, so the tracker settles at an equilibrium *below* ground
            # instead of stopping at it (the position-error spring fighting a
            # velocity term that never relaxes) — confirmed by a smoke test
            # landing at -0.76m instead of 0. Switch to a zero-velocity ground
            # hold once close, so the PD spring settles to rest at 0 and the
            # "is it actually stopped" landed check below can be satisfied.
            near_ground = self.position[i, 2] <= LANDED_ALTITUDE_M * 3
            if near_ground:
                self.target_position[i, 2] = 0.0
                self.target_velocity_ff[i] = (0.0, 0.0, 0.0)
            else:
                self.target_position[i, 2] = self.position[i, 2] - cfg.descent_rate_ms * cfg.dt
                self.target_velocity_ff[i] = (0.0, 0.0, -cfg.descent_rate_ms)
            self.target_accel_ff[i] = 0.0
            self.flight_mode[i] = "LAND"

        flying = self.armed & ~self.landing & ~self.rtl_pending
        for i in np.nonzero(flying)[0]:
            if self.mission_active[i]:
                mode = "SHOW"
            elif self.offboard_active[i]:
                mode = "OFFBOARD"
            else:
                mode = "HOLD"
            self.flight_mode[int(i)] = mode

    # ---------------------------------------------------------- telemetry

    def noisy_positions(self) -> np.ndarray:
        """Every drone's position with sensor-grade noise, as one ``(n, 3)`` array."""
        std = self.config.position_noise_std_m
        if std <= 0:
            return self.position.copy()
        return self.position + self._rng.normal(0.0, std, size=(self.n, 3))

    def noisy_position(self, drone_id: int) -> np.ndarray:
        """Position with sensor-grade noise applied, for telemetry readout only."""
        std = self.config.position_noise_std_m
        if std <= 0:
            return self.position[drone_id].copy()
        return self.position[drone_id] + self._rng.normal(0.0, std, size=3)


def _clamp_norm(vectors: np.ndarray, max_norm: float) -> np.ndarray:
    """Scale each row of ``vectors`` down to at most ``max_norm``, leaving shorter rows untouched."""
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    scale = np.minimum(1.0, max_norm / np.maximum(norms, 1e-9))
    return vectors * scale
