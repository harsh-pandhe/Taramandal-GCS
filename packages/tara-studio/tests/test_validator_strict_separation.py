"""The backend's separation rule is a strict float comparison against 2.0 m.

Studio's final export gate (packages/tara-studio-web/src/engine/sequencer.js,
``buildSequencedShow``, pinned in sequencer.rounding.test.js) uses the same
convention, with no tolerance, so Studio never builds a show this validator
rejects for floating-point reasons. These tests pin the validator side with the
exact coordinates the frontend test uses: two drones 2.00 m apart on the 1 cm
export grid (x = -17.99 and -15.99) whose float distance is a hair under 2.0.

Nothing here relaxes the validator; it documents and locks its behaviour.
"""

import numpy as np

from tara_studio.validator import validate_choreography


def _static_pair(x0: float, x1: float) -> dict:
    def track(x: float) -> list[dict]:
        return [{"time": t, "x": x, "y": 0.0, "z": -30.0, "yaw": 0} for t in (0.0, 1.0, 2.0, 3.0)]

    return {"drones": [{"id": 0, "waypoints": track(x0)}, {"id": 1, "waypoints": track(x1)}]}


def test_grid_pair_two_metres_apart_computes_below_two_and_is_rejected():
    a = np.array([-17.99, 0.0, -30.0])
    b = np.array([-15.99, 0.0, -30.0])
    assert float(np.linalg.norm(a - b)) < 2.0  # 1.9999999999999982

    report = validate_choreography(_static_pair(-17.99, -15.99))

    assert report["status"] == "FAILED"
    assert report["violations"]["proximity"], "the sub-2.0 pair must be reported"
    assert report["metrics"]["min_distance_m"] == 2.0  # the report rounds; the verdict does not


def test_pair_exactly_two_metres_apart_in_floating_point_is_compliant():
    report = validate_choreography(_static_pair(0.0, 2.0))

    assert report["status"] == "PASSED"
    assert not report["violations"]["proximity"]
    assert report["metrics"]["min_distance_m"] == 2.0
