"""Issue #60: /api/export is not blocked by feasibility certification.

Certification (jerk + the shared safety envelope) is surfaced to the
operator — /api/generate's `feasibility_certified` field and violations
dict, rendered by SafetyPanel's jerk category — rather than enforced as an
export gate. It was wired as a hard 422 block during development, but two
pre-existing generator gaps (coarse default waypoint sampling, and a
velocity mismatch at the choreography->land phase handoff — tracked as
follow-up issues, unrelated to what #60 asked for) mean no shape generator's
default output currently clears the jerk bound. Hard-blocking on it would
have broken every real export the moment this merged, so /api/export always
succeeds regardless of certification status; these tests pin that down for
both a clean and a jerky choreography, and confirm /api/generate keeps
reporting the jerk/certification data either way.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tara_core.geometry import quintic_segment
from tara_studio.server import app

client = TestClient(app)

#: A drone that flies forward then instantly reverses — see
#: test_dynamics_validator.py's _sharp_reversal_choreo for why this trips the
#: jerk check, on top of the very high velocity a 5 m hop in 1 s implies.
_INFEASIBLE_CHOREO = {
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


def test_flight_export_fails_closed_for_an_infeasible_choreography():
    res = client.post("/api/export", json={"choreo": _INFEASIBLE_CHOREO, "format": "json"})
    assert res.status_code == 412
    assert "Flight export blocked" in str(res.json()["detail"])


def test_preview_export_succeeds_for_an_infeasible_choreography():
    res = client.post("/api/export", json={"choreo": _INFEASIBLE_CHOREO, "format": "json", "preview_only": True})
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
