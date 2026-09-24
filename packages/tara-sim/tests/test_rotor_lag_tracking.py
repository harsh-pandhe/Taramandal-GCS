"""Issue #60: rotor-lag tracking-error comparison, linear vs. quintic.

Confirms the acceptance criterion that smoothing a transition segment with
``tara_core.geometry.quintic_profile`` (continuous acceleration, hence bounded
jerk) cuts tracking error under a simulated rotor time constant by at least
40% compared to an unsmoothed segment.

"Unsmoothed" here is a plain linear ramp — constant velocity, with a step
change in velocity at both ends of the segment — because that is genuinely
still what ``tara_core.geometry.interpolate_waypoint`` produces for an
ordinary waypoint-to-waypoint segment today (issue #60's own investigation
found no acceleration/jerk continuity anywhere in that function: velocity is
piecewise-constant per segment, so it steps discontinuously at every
boundary). It is a fairer "before" than ``generator._ease``'s old smoothstep
ramp, which was already a partial fix (continuous velocity, discontinuous
acceleration) — comparing quintic against another already-smoothed curve
understates the improvement smoothing an actually sharp transition delivers,
and the two curves' differing peak/mean velocity ratios (see pacing.py's
CRUISE_FRACTION comment) confound "smoother" with "faster" at equal duration.
Linear and quintic share the same mean velocity at equal duration (both cover
the same distance in the same time), so this comparison isolates exactly the
one variable under test: how sharply the commanded acceleration changes.

This reuses ``tara_sim.fleet_state.FleetState``'s existing PD setpoint
tracker rather than building a second simulation, with
``SimConfig.rotor_tau_s`` (added alongside this test) supplying the
first-order motor-lag term neither the tracker nor the deterministic
validator previously modelled.
"""

from __future__ import annotations

import math

from tara_core.geometry import quintic_profile
from tara_sim.fleet_state import FleetState, SimConfig

#: Representative rotor spool-up/down time constant, per the issue.
ROTOR_TAU_S = 0.08

SEGMENT_DURATION_S = 2.0
TICK_HZ = 200.0

P0 = (0.0, 0.0, 0.0)
P1 = (10.0, 4.0, 6.0)

#: Minimum fractional drop in tracking error the quintic profile must show
#: over the linear one, per the issue's acceptance criteria.
MIN_REDUCTION = 0.40


def _linear(r: float) -> tuple[float, float, float]:
    """Position/velocity/acceleration of a plain constant-velocity ramp at
    ``r`` — what ``interpolate_waypoint`` still produces for a waypoint
    segment today. Velocity steps from 0 to 1 (normalized) the instant the
    segment starts, and back to 0 the instant it ends.
    """
    r = min(1.0, max(0.0, r))
    return r, 1.0, 0.0


def _quintic(r: float) -> tuple[float, float, float]:
    """Position/velocity/acceleration of the minimum-jerk ramp at ``r``."""
    pos, vel, acc, _jerk = quintic_profile(r)
    return pos, vel, acc


def _mean_tracking_error(shape_fn) -> float:
    """Streams a P0->P1 transition shaped by ``shape_fn`` through one
    simulated drone and returns its mean position-tracking error under a
    simulated rotor lag.

    ``setpoint_latency_s`` and the velocity/accel caps are neutralised, and
    the tracker's PD gains are left at ``SimConfig``'s production defaults,
    so the only thing distinguishing the two curves is what's under test: how
    sharply each one's commanded acceleration changes, and how much of that
    the rotor-lag filter cannot instantly follow.
    """
    config = SimConfig(
        tick_hz=TICK_HZ,
        setpoint_latency_s=0.0,
        wind_std_ms2=0.0,
        position_noise_std_m=0.0,
        rotor_tau_s=ROTOR_TAU_S,
        max_accel_ms2=1000.0,
        max_velocity_ms=1000.0,
    )
    fs = FleetState(1, config=config)
    fs.command_arm(0)
    dt = config.dt

    delta = tuple(b - a for a, b in zip(P0, P1))
    errors: list[float] = []
    t = 0.0
    while t <= SEGMENT_DURATION_S:
        r = t / SEGMENT_DURATION_S
        pos_s, vel_s, acc_s = shape_fn(r)
        target_pos = tuple(a + d * pos_s for a, d in zip(P0, delta))
        target_vel = tuple(d * vel_s / SEGMENT_DURATION_S for d in delta)
        target_acc = tuple(d * acc_s / SEGMENT_DURATION_S**2 for d in delta)

        fs.command_setpoint(0, target_pos, velocity_ff=target_vel, accel_ff=target_acc)
        fs.tick(dt)

        errors.append(math.dist(tuple(fs.position[0]), target_pos))
        t += dt

    return sum(errors) / len(errors)


def test_quintic_smoothing_cuts_rotor_lag_tracking_error_by_at_least_40_percent():
    linear_error = _mean_tracking_error(_linear)
    quintic_error = _mean_tracking_error(_quintic)

    reduction = 1.0 - quintic_error / linear_error
    assert quintic_error <= linear_error * (1.0 - MIN_REDUCTION), (
        f"expected >={MIN_REDUCTION * 100:.0f}% tracking-error reduction: "
        f"linear={linear_error:.4f}m, quintic={quintic_error:.4f}m "
        f"({reduction * 100:.1f}% reduction)"
    )
