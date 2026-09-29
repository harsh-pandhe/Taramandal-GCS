"""Aerodynamic power and battery endurance validation (issue #54).

A choreography can be kinematically clean (no collisions, no speed or
acceleration violations) and still ground the fleet: every waypoint costs
energy, and a show that drains the packs below their safe floor ends in
low-battery failsafe landings mid-performance. This module estimates, for
every drone and every instant of a show:

1. **Electrical power**, from quadrotor momentum theory:

   * Thrust is what holds the drone up and accelerates it, plus what overcomes
     body drag: ``T = m (a + g) + 1/2 rho CdA |v| v``.
   * **Induced power** ``kappa T v_i``, with the induced velocity ``v_i`` from
     the general (Glauert) momentum equation
     ``v_i = v_h^2 / sqrt(V_edge^2 + (V_axial + v_i)^2)``, where
     ``v_h = sqrt(T / 2 rho A)``. Forward flight and climb both increase the
     mass flow through the disk and so *reduce* ``v_i``.
   * **Profile power** of the spinning blades,
     ``sigma Cd0 / 8 * rho A (Omega R)^3 (1 + 4.65 mu^2)``, with tip speed
     scaling as ``sqrt(T)``.
   * **Useful work**, ``T . v``: climbing, speeding up, and pushing air aside
     (parasite drag). It is clamped at zero, because descending or braking
     does not put charge back into the pack.
   * Divided by the motor/ESC efficiency, plus a constant avionics and LED draw.

2. **Battery discharge**. Each chemistry (:data:`CHEMISTRIES`) has an
   open-circuit voltage curve over state of charge, and an internal resistance
   that sags the terminal voltage under load. Both, and the usable capacity,
   are corrected for ambient temperature. Current is whatever delivers the
   required power at the sagged voltage, ``P = (V_oc - I R) I``.

The show is then **UNSAFE** (``status == "FAILED"``) if any drone:

* goes past the allowed depth of discharge (70% by default). The report says
  how many seconds the show must lose to fit.
* exceeds the pack's continuous C-rating.
* sags below the chemistry's minimum loaded cell voltage, which is where the
  flight controller's low-battery failsafe forces a landing.

Deliberate simplifications, all of which make the estimate *pessimistic*:

* Still air: no wind.
* Descent is costed like hover. Momentum theory would credit a descending
  rotor with less power, but slow descents run through the vortex-ring state,
  where the real power is higher, not lower.
* No regeneration.
* Acceleration is smoothed over :data:`ACCEL_SMOOTHING_S` before it drives
  thrust. Linearly interpolated waypoints have velocity steps at every
  waypoint, whose raw second difference is an impulse no airframe can follow.
  Left unsmoothed, those impulses would dominate the peak-current check with
  numbers that are artefacts of the sampling.

Coordinates follow :mod:`tara_core.show`: NED metres, ``z`` negative up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import uniform_filter1d

from tara_core.geometry import sweep_times
from tara_core.show import Show

__all__ = [
    "CHEMISTRIES",
    "DEFAULT_AIRFRAME",
    "DEFAULT_BATTERY",
    "AirframeSpec",
    "BatterySpec",
    "Chemistry",
    "EnduranceLimits",
    "Environment",
    "air_density",
    "hover_power_w",
    "induced_velocity",
    "validate_energy",
]

GRAVITY_MS2 = 9.80665
_R_DRY_AIR = 287.05  # J / (kg K)
_SEA_LEVEL_PA = 101325.0

#: Window over which commanded acceleration is averaged before it drives thrust
#: (see module docstring).
ACCEL_SMOOTHING_S = 0.5

#: A drone below this altitude and slower than this speed is sitting on its pad
#: with motors idle: it draws avionics power only.
_GROUND_ALT_M = 0.05
_GROUND_SPEED_MS = 0.05

#: Drones simulated per vectorized batch. Bounds memory at large fleet sizes
#: (a 1000-drone, 10-minute show is 60M samples per array otherwise).
_DRONE_CHUNK = 128

#: Points per series in the timeline sent to the UI graph.
_TIMELINE_POINTS = 400

#: Violations retained per category, matching the other validators.
_MAX_REPORTED = 50


@dataclass(frozen=True)
class AirframeSpec:
    """Physical parameters of one drone.

    Defaults approximate the Holybro X500-class quad that the PX4 SITL fleet
    (``gz_x500``) flies: 2.0 kg with battery, four 10-inch props. They are a
    stand-in until the real show airframe is characterised, and every field
    can be overridden per request.

    Attributes:
        mass_kg: All-up mass, battery included.
        rotor_count: Number of rotors.
        rotor_radius_m: Propeller radius.
        induced_power_factor: ``kappa``, the multiplier on ideal induced power
            for tip losses and non-uniform inflow (typically 1.1-1.2).
        blade_solidity: Blade area / disk area, ``sigma``.
        blade_drag_coeff: Mean blade profile drag coefficient, ``Cd0``.
        hover_tip_speed_ms: Blade tip speed at hover thrust, ``Omega R``.
        drag_area_m2: Body drag coefficient times frontal area, ``Cd A``.
        drivetrain_efficiency: Motor and ESC efficiency, electrical to shaft.
        avionics_power_w: Constant draw of flight controller, radio, companion
            computer and show LEDs, whether or not the motors spin.
    """

    mass_kg: float = 2.0
    rotor_count: int = 4
    rotor_radius_m: float = 0.127
    induced_power_factor: float = 1.15
    blade_solidity: float = 0.10
    blade_drag_coeff: float = 0.012
    hover_tip_speed_ms: float = 73.0
    drag_area_m2: float = 0.05
    drivetrain_efficiency: float = 0.80
    avionics_power_w: float = 10.0

    @property
    def disk_area_m2(self) -> float:
        return self.rotor_count * math.pi * self.rotor_radius_m ** 2


@dataclass(frozen=True)
class Chemistry:
    """Discharge characteristics of one cell chemistry.

    The curves are representative published figures for the chemistry, not
    one specific cell. A pack that has been characterised should override
    ``internal_resistance_ohm`` and ``max_c_rate`` on :class:`BatterySpec`.

    Attributes:
        name: Display name.
        ocv_soc: State-of-charge breakpoints, 0-1, ascending.
        ocv_v: Open-circuit cell voltage at each breakpoint.
        resistance_ohm_ah: Cell internal resistance times capacity. A cell's
            resistance at 25 C is this divided by its capacity in Ah, since
            bigger cells of the same construction have proportionally lower
            resistance.
        min_cell_voltage_v: Loaded voltage at which the flight controller's
            low-battery failsafe forces a landing.
        max_c_rate: Default continuous discharge rating.
        temp_c: Ambient temperature breakpoints, C, ascending.
        capacity_factor: Usable fraction of rated capacity at each temperature.
        resistance_factor: Internal resistance multiplier at each temperature.
    """

    name: str
    ocv_soc: tuple[float, ...]
    ocv_v: tuple[float, ...]
    resistance_ohm_ah: float
    min_cell_voltage_v: float
    max_c_rate: float
    temp_c: tuple[float, ...]
    capacity_factor: tuple[float, ...]
    resistance_factor: tuple[float, ...]


_TEMP_C = (-20.0, -10.0, 0.0, 10.0, 25.0, 40.0)
_RESISTANCE_FACTOR = (3.0, 2.2, 1.6, 1.25, 1.0, 0.9)
_SOC = (0.0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.0)

CHEMISTRIES: dict[str, Chemistry] = {
    "lipo": Chemistry(
        name="LiPo",
        ocv_soc=_SOC,
        ocv_v=(3.27, 3.61, 3.69, 3.73, 3.77, 3.79, 3.82, 3.87, 3.92, 3.97, 4.10, 4.20),
        resistance_ohm_ah=0.015,
        min_cell_voltage_v=3.5,
        max_c_rate=25.0,
        temp_c=_TEMP_C,
        capacity_factor=(0.60, 0.72, 0.83, 0.92, 1.0, 1.0),
        resistance_factor=_RESISTANCE_FACTOR,
    ),
    "liion": Chemistry(
        name="Li-ion",
        ocv_soc=_SOC,
        ocv_v=(3.00, 3.30, 3.45, 3.55, 3.62, 3.68, 3.74, 3.82, 3.90, 3.98, 4.08, 4.20),
        resistance_ohm_ah=0.10,
        min_cell_voltage_v=3.0,
        max_c_rate=8.0,
        temp_c=_TEMP_C,
        capacity_factor=(0.65, 0.75, 0.85, 0.93, 1.0, 1.0),
        resistance_factor=_RESISTANCE_FACTOR,
    ),
}


@dataclass(frozen=True)
class BatterySpec:
    """The pack each drone flies on.

    Attributes:
        chemistry: Key into :data:`CHEMISTRIES`.
        cells_series: Cells in series (the ``S`` in 4S).
        capacity_mah: Rated pack capacity.
        max_c_rate: Continuous discharge rating. ``None`` uses the chemistry's
            default.
        internal_resistance_ohm: Per-cell resistance at 25 C. ``None`` derives
            it from the chemistry and capacity.
        initial_soc: State of charge at the start of the show, 0-1.
    """

    chemistry: str = "lipo"
    cells_series: int = 4
    capacity_mah: float = 5000.0
    max_c_rate: float | None = None
    internal_resistance_ohm: float | None = None
    initial_soc: float = 1.0

    def __post_init__(self):
        if self.chemistry not in CHEMISTRIES:
            raise ValueError(
                f"unknown battery chemistry {self.chemistry!r}; expected one of {sorted(CHEMISTRIES)}"
            )
        if self.cells_series < 1:
            raise ValueError(f"cells_series must be at least 1, got {self.cells_series}")
        if self.capacity_mah <= 0:
            raise ValueError(f"capacity_mah must be positive, got {self.capacity_mah}")
        if not 0.0 < self.initial_soc <= 1.0:
            raise ValueError(f"initial_soc must be in (0, 1], got {self.initial_soc}")

    @property
    def spec(self) -> Chemistry:
        return CHEMISTRIES[self.chemistry]

    @property
    def capacity_ah(self) -> float:
        return self.capacity_mah / 1000.0

    @property
    def rated_c_rate(self) -> float:
        return self.max_c_rate if self.max_c_rate is not None else self.spec.max_c_rate

    @property
    def cell_resistance_ohm(self) -> float:
        if self.internal_resistance_ohm is not None:
            return self.internal_resistance_ohm
        return self.spec.resistance_ohm_ah / self.capacity_ah


@dataclass(frozen=True)
class Environment:
    """Conditions at the venue.

    Attributes:
        ambient_temp_c: Air and battery temperature. Drives air density, usable
            capacity and internal resistance.
        venue_altitude_m: Height above sea level. Thinner air costs induced power.
    """

    ambient_temp_c: float = 25.0
    venue_altitude_m: float = 0.0


@dataclass(frozen=True)
class EnduranceLimits:
    """Pass/fail thresholds.

    Attributes:
        max_depth_of_discharge: Largest fraction of the (temperature-corrected)
            usable capacity a show may consume. 0.70 leaves a 30% reserve for
            holds, RTL and landing on a cold or ageing pack.
    """

    max_depth_of_discharge: float = 0.70


DEFAULT_AIRFRAME = AirframeSpec()
DEFAULT_BATTERY = BatterySpec()


def air_density(temp_c: float, altitude_m: float = 0.0) -> float:
    """Air density, kg/m^3, from the ISA pressure lapse and the ideal gas law."""
    pressure = _SEA_LEVEL_PA * (1.0 - 2.25577e-5 * altitude_m) ** 5.25588
    return pressure / (_R_DRY_AIR * (temp_c + 273.15))


def induced_velocity(thrust, rho: float, disk_area: float, v_axial, v_edge, iterations: int = 12):
    """Momentum-theory induced velocity through the rotor disks.

    Solves ``v_i * sqrt(V_edge^2 + (V_axial + v_i)^2) = v_h^2`` by Newton's
    method, vectorized. ``V_axial`` is airspeed along the thrust axis (positive
    when climbing into the rotor's own inflow). It is clamped at zero, so
    descent is costed like hover (see module docstring).

    Args:
        thrust: Total thrust, N. Scalar or array.
        rho: Air density, kg/m^3.
        disk_area: Total disk area of all rotors, m^2.
        v_axial: Airspeed component along the thrust axis, m/s.
        v_edge: Airspeed component in the rotor plane, m/s.
        iterations: Newton steps. Starting from ``v_h``, which is at or above
            the root, convergence is monotone and a dozen steps is plenty.

    Returns:
        Induced velocity, m/s, broadcast to the inputs' shape.
    """
    thrust = np.maximum(np.asarray(thrust, dtype=float), 0.0)
    v_axial = np.maximum(np.asarray(v_axial, dtype=float), 0.0)
    v_edge = np.asarray(v_edge, dtype=float)
    vh2 = thrust / (2.0 * rho * disk_area)
    v = np.sqrt(vh2)
    for _ in range(iterations):
        root = np.sqrt(v_edge ** 2 + (v_axial + v) ** 2)
        f = v * root - vh2
        fprime = root + v * (v_axial + v) / np.maximum(root, 1e-9)
        v = np.maximum(v - f / np.maximum(fprime, 1e-9), 0.0)
    return v


def _electrical_power(vel: np.ndarray, acc: np.ndarray, airborne: np.ndarray,
                      airframe: AirframeSpec, rho: float) -> np.ndarray:
    """Electrical power, W, for arrays of ENU velocity/acceleration ``(..., 3)``."""
    m = airframe.mass_kg
    speed = np.linalg.norm(vel, axis=-1)

    # Thrust must supply m(a + g) and cancel body drag (which acts along -v).
    thrust_vec = m * acc
    thrust_vec[..., 2] += m * GRAVITY_MS2
    thrust_vec += 0.5 * rho * airframe.drag_area_m2 * speed[..., None] * vel
    thrust = np.linalg.norm(thrust_vec, axis=-1)

    axis = thrust_vec / np.maximum(thrust, 1e-9)[..., None]
    v_axial = np.sum(vel * axis, axis=-1)
    v_edge = np.linalg.norm(vel - v_axial[..., None] * axis, axis=-1)

    v_i = induced_velocity(thrust, rho, airframe.disk_area_m2, v_axial, v_edge)
    p_induced = airframe.induced_power_factor * thrust * v_i

    tip = airframe.hover_tip_speed_ms * np.sqrt(thrust / (m * GRAVITY_MS2))
    mu = v_edge / np.maximum(tip, 1.0)
    p_profile = (
        airframe.blade_solidity * airframe.blade_drag_coeff / 8.0
        * rho * airframe.disk_area_m2 * tip ** 3 * (1.0 + 4.65 * mu ** 2)
    )

    # T . v covers climbing, accelerating and parasite drag in one term.
    p_work = np.maximum(np.sum(thrust_vec * vel, axis=-1), 0.0)

    p_shaft = p_induced + p_profile + p_work
    return np.where(airborne, p_shaft / airframe.drivetrain_efficiency, 0.0) + airframe.avionics_power_w


def hover_power_w(airframe: AirframeSpec = DEFAULT_AIRFRAME, env: Environment = Environment()) -> float:
    """Electrical power to hold a steady hover, W."""
    zero = np.zeros((1, 3))
    power = _electrical_power(zero, zero.copy(), np.ones(1, dtype=bool), airframe,
                              air_density(env.ambient_temp_c, env.venue_altitude_m))
    return float(power[0])


def _simulate_battery(power: np.ndarray, battery: BatterySpec, env: Environment, dt: float):
    """Discharge each drone's pack along its power profile.

    Current and state of charge are coupled: current depends on the sagged
    terminal voltage, which depends on how much charge has already gone. The
    dependence is weak, so rather than stepping sample by sample in Python, the
    whole ``(drones, samples)`` array is solved as a fixed point. Guess the
    current, integrate it into SoC, recompute current from the voltage at that
    SoC, and repeat until nothing moves.

    Returns:
        ``(current_a, soc, cell_voltage_v)``, each shaped like ``power``.
        ``soc`` is the state of charge at the start of each sample.
    """
    chem = battery.spec
    cells = battery.cells_series
    temp = env.ambient_temp_c
    capacity_ah = battery.capacity_ah * float(np.interp(temp, chem.temp_c, chem.capacity_factor))
    r_pack = cells * battery.cell_resistance_ohm * float(np.interp(temp, chem.temp_c, chem.resistance_factor))

    def open_circuit(soc):
        return cells * np.interp(np.clip(soc, 0.0, 1.0), chem.ocv_soc, chem.ocv_v)

    current = power / open_circuit(np.full(power.shape, battery.initial_soc))
    for _ in range(50):
        drawn_ah = np.cumsum(current, axis=1) * dt / 3600.0 - current * dt / 3600.0
        soc = battery.initial_soc - drawn_ah / capacity_ah
        voc = open_circuit(soc)
        if r_pack > 0:
            disc = voc ** 2 - 4.0 * r_pack * power
            # A negative discriminant means the pack cannot deliver this power
            # at any current: the voltage has collapsed. Take the maximum-power
            # point (V = Voc / 2); the voltage check then flags it.
            new = np.where(disc >= 0, (voc - np.sqrt(np.maximum(disc, 0.0))) / (2.0 * r_pack), voc / (2.0 * r_pack))
        else:
            new = power / voc
        converged = np.max(np.abs(new - current), initial=0.0) < 1e-6
        current = new
        if converged:
            break

    cell_voltage = (voc - current * r_pack) / cells
    return current, soc, cell_voltage


def _sample_positions(show: Show, drone_ids: list[int], times: np.ndarray) -> np.ndarray:
    """ENU positions ``(drones, samples, 3)``.

    Same linear, end-clamped interpolation as
    :func:`tara_core.geometry.interpolate_waypoint`, vectorized.
    """
    out = np.empty((len(drone_ids), len(times), 3))
    for row, drone_id in enumerate(drone_ids):
        wps = show.drones[drone_id]
        t = np.array([wp["time"] for wp in wps], dtype=float)
        out[row, :, 0] = np.interp(times, t, [wp["x"] for wp in wps])
        out[row, :, 1] = np.interp(times, t, [wp["y"] for wp in wps])
        out[row, :, 2] = -np.interp(times, t, [wp["z"] for wp in wps])  # NED down -> up
    return out


def _downsample(times: np.ndarray, series: dict[str, np.ndarray], reduce: dict[str, str]) -> dict:
    """Thin each series to about :data:`_TIMELINE_POINTS`, keeping the last sample.

    ``reduce`` picks ``"min"`` or ``"max"`` per series, so a brief current spike
    or voltage dip between kept points is not lost from the graph.
    """
    n = len(times)
    stride = max(1, math.ceil(n / _TIMELINE_POINTS))
    starts = list(range(0, n, stride))
    out = {"time_s": [round(float(times[min(s + stride, n) - 1]), 2) for s in starts]}
    for key, values in series.items():
        agg = np.min if reduce[key] == "min" else np.max
        out[key] = [round(float(agg(values[s:s + stride])), 2) for s in starts]
    return out


def validate_energy(
    choreo_data,
    airframe: AirframeSpec = DEFAULT_AIRFRAME,
    battery: BatterySpec = DEFAULT_BATTERY,
    environment: Environment = Environment(),
    limits: EnduranceLimits = EnduranceLimits(),
    dt: float = 0.1,
) -> dict:
    """Estimate every drone's power draw and battery state over a show.

    Args:
        choreo_data: A :class:`~tara_core.show.Show` or either supported dict shape.
        airframe: Drone physical parameters.
        battery: Pack each drone carries.
        environment: Ambient temperature and venue altitude.
        limits: Depth-of-discharge threshold.
        dt: Sweep interval, seconds. Same default as the other validators.

    Returns:
        A report with the shape the other validators use (``status``,
        ``summary``, ``metrics``, ``violations``), plus a downsampled
        ``timeline`` of fleet battery state for the UI graph.
        ``metrics["required_duration_reduction_s"]`` is how much shorter the
        show must be for its worst drone to stay within the depth-of-discharge
        limit, or 0 if it already does.
    """
    show = Show.from_any(choreo_data)
    drone_ids = sorted(d for d, wps in show.drones.items() if wps)
    violations: dict[str, list] = {"depth_of_discharge": [], "c_rate": [], "voltage": []}

    if not drone_ids:
        return {
            "status": "FAILED",
            "summary": "No drones found in choreography.",
            "metrics": {},
            "violations": violations,
            "timeline": {},
        }

    chem = battery.spec
    rho = air_density(environment.ambient_temp_c, environment.venue_altitude_m)
    usable_ah = battery.capacity_ah * float(
        np.interp(environment.ambient_temp_c, chem.temp_c, chem.capacity_factor)
    )
    c_limit = battery.rated_c_rate
    dod_limit = limits.max_depth_of_discharge
    times = np.asarray(sweep_times(show.duration, dt))
    n_samples = len(times)
    smoothing = max(1, round(ACCEL_SMOOTHING_S / dt))

    fleet_min_soc = np.full(n_samples, np.inf)
    fleet_soc_sum = np.zeros(n_samples)
    fleet_max_current = np.zeros(n_samples)
    total_energy_wh = 0.0
    per_drone = []  # (drone_id, energy_wh, final_dod, peak_current, peak_t, min_v, min_v_t)

    for start in range(0, len(drone_ids), _DRONE_CHUNK):
        ids = drone_ids[start:start + _DRONE_CHUNK]
        pos = _sample_positions(show, ids, times)
        if n_samples > 1:
            vel = np.gradient(pos, dt, axis=1)
            acc = uniform_filter1d(np.gradient(vel, dt, axis=1), size=smoothing, axis=1, mode="nearest")
        else:
            vel = np.zeros_like(pos)
            acc = np.zeros_like(pos)
        airborne = (pos[..., 2] > _GROUND_ALT_M) | (np.linalg.norm(vel, axis=-1) > _GROUND_SPEED_MS)

        power = _electrical_power(vel, acc, airborne, airframe, rho)
        current, soc, cell_v = _simulate_battery(power, battery, environment, dt)
        # SoC after the last sample's draw, which is what the show ends on.
        final_soc = soc[:, -1] - current[:, -1] * dt / 3600.0 / usable_ah
        energy_wh = np.sum(power, axis=1) * dt / 3600.0
        c_rate = current / battery.capacity_ah

        fleet_min_soc = np.minimum(fleet_min_soc, soc.min(axis=0))
        fleet_soc_sum += soc.sum(axis=0)
        fleet_max_current = np.maximum(fleet_max_current, current.max(axis=0))
        total_energy_wh += float(energy_wh.sum())

        for row, drone_id in enumerate(ids):
            dod = float(1.0 - final_soc[row])
            peak_idx = int(np.argmax(current[row]))
            min_v_idx = int(np.argmin(cell_v[row]))
            per_drone.append((drone_id, float(energy_wh[row]), float(dod), float(current[row, peak_idx]),
                              float(times[peak_idx]), float(cell_v[row, min_v_idx]), float(times[min_v_idx])))

            if dod > dod_limit:
                crossed = np.nonzero(1.0 - soc[row] > dod_limit)[0]
                cross_t = float(times[crossed[0]]) if len(crossed) else float(times[-1])
                # Cutting the show removes flight at roughly its average draw,
                # so the excess charge divided by the mean airborne current is
                # how many seconds must go.
                flying = airborne[row]
                mean_current = float(current[row][flying].mean()) if flying.any() else float(current[row].mean())
                excess_ah = (dod - dod_limit) * usable_ah
                violations["depth_of_discharge"].append({
                    "drone": drone_id,
                    "time": round(cross_t, 2),
                    "depth_of_discharge_pct": round(dod * 100.0, 1),
                    "limit_pct": round(dod_limit * 100.0, 1),
                    "required_reduction_s": math.ceil(excess_ah * 3600.0 / mean_current),
                })

            over_c = np.nonzero(c_rate[row] > c_limit)[0]
            if len(over_c):
                violations["c_rate"].append({
                    "drone": drone_id,
                    "time": round(float(times[over_c[0]]), 2),
                    "c_rate": round(float(c_rate[row].max()), 2),
                    "current_a": round(float(current[row].max()), 2),
                    "limit": c_limit,
                })

            under_v = np.nonzero(cell_v[row] < chem.min_cell_voltage_v)[0]
            if len(under_v):
                violations["voltage"].append({
                    "drone": drone_id,
                    "time": round(float(times[under_v[0]]), 2),
                    "cell_voltage_v": round(float(cell_v[row].min()), 3),
                    "limit_v": chem.min_cell_voltage_v,
                })

    required_reduction = max((v["required_reduction_s"] for v in violations["depth_of_discharge"]), default=0)
    for key in violations:
        violations[key] = sorted(violations[key], key=lambda v: (v["time"], v["drone"]))[:_MAX_REPORTED]

    worst = max(per_drone, key=lambda d: d[2])
    peak = max(per_drone, key=lambda d: d[3])
    sag = min(per_drone, key=lambda d: d[5])
    duration = float(times[-1])

    problems = []
    if violations["depth_of_discharge"]:
        problems.append(
            f"drone {worst[0]} uses {worst[2] * 100:.0f}% of its battery (limit {dod_limit * 100:.0f}%); "
            f"shorten the show by at least {required_reduction}s"
        )
    if violations["c_rate"]:
        problems.append(f"peak draw {peak[3]:.1f}A exceeds the pack's {c_limit:g}C rating")
    if violations["voltage"]:
        problems.append(
            f"cell voltage sags to {sag[5]:.2f}V, below the {chem.min_cell_voltage_v:g}V failsafe floor"
        )
    failed = bool(problems)

    return {
        "status": "FAILED" if failed else "PASSED",
        "summary": (
            "UNSAFE: " + "; ".join(problems) + "."
            if failed
            else f"Battery endurance OK: worst drone ends with {(1 - worst[2]) * 100:.0f}% remaining."
        ),
        "metrics": {
            "total_energy_wh": round(total_energy_wh, 2),
            "max_drone_energy_wh": round(max(d[1] for d in per_drone), 2),
            "peak_current_a": round(peak[3], 2),
            "peak_current_drone": peak[0],
            "peak_current_time_s": round(peak[4], 2),
            "peak_c_rate": round(peak[3] / battery.capacity_ah, 2),
            "max_depth_of_discharge_pct": round(worst[2] * 100.0, 1),
            "min_remaining_pct": round((1.0 - worst[2]) * 100.0, 1),
            "worst_drone": worst[0],
            "min_cell_voltage_v": round(sag[5], 3),
            "hover_power_w": round(hover_power_w(airframe, environment), 1),
            "usable_capacity_mah": round(usable_ah * 1000.0),
            "duration_s": round(duration, 2),
            "required_duration_reduction_s": required_reduction,
            "max_safe_duration_s": round(max(duration - required_reduction, 0.0), 1),
        },
        "limits": {
            "max_depth_of_discharge_pct": round(dod_limit * 100.0, 1),
            "max_c_rate": c_limit,
            "min_cell_voltage_v": chem.min_cell_voltage_v,
        },
        "violations": violations,
        "timeline": _downsample(
            times,
            {
                "min_remaining_pct": np.clip(fleet_min_soc, 0.0, 1.0) * 100.0,
                "mean_remaining_pct": np.clip(fleet_soc_sum / len(drone_ids), 0.0, 1.0) * 100.0,
                "max_current_a": fleet_max_current,
            },
            {"min_remaining_pct": "min", "mean_remaining_pct": "min", "max_current_a": "max"},
        ),
    }
