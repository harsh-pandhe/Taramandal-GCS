"""Issue #60 / #143 / #157: how /api/export treats failed feasibility certification.

#60's original decision was that /api/export is never blocked by
certification (jerk + the shared safety envelope) at all — surfaced to the
operator via /api/generate's `feasibility_certified` field and SafetyPanel's
jerk category instead, because two pre-existing generator gaps (coarse
default waypoint sampling, and a velocity mismatch at the choreography->land
phase handoff) meant no shape generator's default output cleared the jerk
bound, and hard-blocking would have broken every real export.

#143 supersedes that for flight (non-preview) exports: they now require full
certification (core safety PASSED and 0 jerk violations), fail-closed with
412 otherwise. Preview exports (issue #143) still require core safety -
proximity, velocity, acceleration, yaw rate, altitude - but are exempt from
the jerk bound specifically, since that's a choreography-quality metric, not
a safety one, and letting an operator preview a jerk-failing show is what
preview mode is for.

The generator gaps behind the jerk failures are still open (tracked as
#157) - investigated directly for this update: raising the sampling rate
2Hz -> 10Hz barely moves max jerk (129 -> ~65-70 m/s^3, still far over the
20 m/s^3 limit), so it is not primarily a sampling-coarseness artifact. It is
a genuine velocity discontinuity at the phase handoff that differentiation
can't smooth out by sampling more finely - fixing it means velocity-matching
that handoff (the same quintic-easing technique _quintic_smoothed_choreo
below already uses) across all five shape generators, not a generator
default tweak. Scoped as its own fix, not folded in here.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tara_core.geometry import quintic_segment
from tara_studio import generator
from tara_studio.server import app

client = TestClient(app)

#: A drone that flies forward then instantly reverses — see
#: test_dynamics_validator.py's _sharp_reversal_choreo for why this trips the
#: jerk check. Also trips core safety: the instantaneous reversal is a ~100
#: m/s^2 spike over one dt=0.1 sample, over the 50 m/s^2 acceleration
#: ceiling, not just the stricter jerk bound - so unlike a merely
#: jerk-infeasible show, this one is blocked in preview too (issue #143).
_UNSAFE_CHOREO = {
    "drones": [
        {
            "id": 0,
            "waypoints": [
                {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                {"time": 1.0, "x": 5.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                {"time": 2.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
            ],
        }
    ]
}


def _jerk_infeasible_but_core_safe_choreo():
    """A real generator's default output: 0 core-safety violations, ~50 jerk
    violations (confirmed directly against certify_feasibility_report) —
    exactly the shape preview mode's jerk exemption exists for.
    """
    return generator.generate_circle(3, 30.0, 2.0)


def _quintic_smoothed_choreo():
    """A densely-sampled, jerk-continuous single-drone hop.

    Deliberately not one of the five shape generators: even after issue #60's
    _ease fix, their *default* sampling rate (a handful of Hz) is too coarse
    for validate_dynamics's jerk check to clear — the phase-transition ramps
    are analytically smooth now, but the piecewise-linear interpolation
    between sparse waypoints still has jerky corners at that resolution, and
    (separately) the handoff from the choreography phase's steady orbital
    velocity into the land phase's from-rest ease ramp isn't itself velocity-
    matched. Both are real, pre-existing gaps in generator.py's phase
    stitching, orthogonal to the smoothing this issue adds — tracked as
    follow-up rather than fixed here.
    """
    p0, p1 = (0.0, 0.0, -2.0), (5.0, 0.0, -2.0)
    half = 6.0
    dt = 1.0 / 20.0
    waypoints = []
    t = 0.0
    while t <= 2 * half + 1e-9:
        state = quintic_segment(p0, p1, half, t) if t <= half else quintic_segment(p1, p0, half, t - half)
        x, y, z = state["position"]
        waypoints.append({"time": round(t, 4), "x": x, "y": y, "z": z, "yaw": 0.0})
        t += dt
    return {"drones": [{"id": 0, "waypoints": waypoints}]}


def test_flight_export_fails_closed_for_an_unsafe_choreography():
    res = client.post("/api/export", json={"choreo": _UNSAFE_CHOREO, "format": "json"})
    assert res.status_code == 412
    assert "Flight export blocked" in str(res.json()["detail"])


def test_preview_export_still_blocked_for_a_core_unsafe_choreography():
    # Unlike a merely jerk-infeasible show, this one also breaches core
    # safety (acceleration) - preview mode's jerk exemption does not cover it.
    res = client.post("/api/export", json={"choreo": _UNSAFE_CHOREO, "format": "json", "preview_only": True})
    assert res.status_code == 412
    assert "core safety" in str(res.json()["detail"]).lower()


def test_flight_export_fails_closed_for_a_jerk_only_infeasible_choreography():
    res = client.post("/api/export", json={"choreo": _jerk_infeasible_but_core_safe_choreo(), "format": "json"})
    assert res.status_code == 412
    assert "Flight export blocked" in str(res.json()["detail"])


def test_preview_export_succeeds_for_a_jerk_only_infeasible_choreography():
    res = client.post(
        "/api/export",
        json={"choreo": _jerk_infeasible_but_core_safe_choreo(), "format": "json", "preview_only": True},
    )
    assert res.status_code == 200
    assert res.json()["preview_only"] is True


def test_export_succeeds_for_a_certified_choreography():
    res = client.post("/api/export", json={"choreo": _quintic_smoothed_choreo(), "format": "json"})
    assert res.status_code == 200


def test_generate_response_reports_jerk_and_certification_for_a_real_shape():
    # A real generator's default output is expected to be jerky today (see
    # the module docstring) — this pins down that /api/generate still
    # surfaces that honestly, rather than the lack of an export gate hiding
    # it from the operator.
    res = client.post(
        "/api/generate",
        json={"shape": "circle", "num_drones": 3, "duration": 30.0, "rate": 2.0},
    )
    assert res.status_code == 200
    validation = res.json()["validation"]
    assert "jerk" in validation["violations"]
    assert "max_jerk_m_s3" in validation["metrics"]
    assert validation["feasibility_certified"] is False
    assert validation["violations"]["jerk"], "expected the default circle output to still read as jerky"
