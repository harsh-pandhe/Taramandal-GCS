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
        dt = self.config.dt
        try:
            while self._running:
                self.tick(dt)
                await asyncio.sleep(dt)
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
        self.landing[drone_id] = False
        self.rtl_pending[drone_id] = False

    def command_land(self, drone_id: int) -> None:
        """Descend straight down from the current position, then disarm."""
        self.rtl_pending[drone_id] = False
        self.landing[drone_id] = True

    def command_rtl(self, drone_id: int) -> None:
        """Fly to (0, 0) at the current altitude, then land."""
        self.landing[drone_id] = False
        self.rtl_pending[drone_id] = True

    def command_arm(self, drone_id: int) -> None:
        self.armed[drone_id] = True

    def command_disarm(self, drone_id: int) -> None:
        self.armed[drone_id] = False
        self.offboard_active[drone_id] = False
        self.landing[drone_id] = False
        self.rtl_pending[drone_id] = False

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

        self._apply_landing_and_rtl()

        pos_err = self.target_position - self.position
        vel_err = self.target_velocity_ff - self.velocity
        accel = cfg.kp * pos_err + cfg.kd * vel_err + self.target_accel_ff
        if cfg.wind_std_ms2 > 0:
            accel = accel + self._rng.normal(0.0, cfg.wind_std_ms2, size=(self.n, 3))
        accel = _clamp_norm(accel, cfg.max_accel_ms2)

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
            self.flight_mode[int(i)] = "OFFBOARD" if self.offboard_active[i] else "HOLD"

    # ---------------------------------------------------------- telemetry

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
