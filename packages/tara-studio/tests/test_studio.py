import pytest
from tara_core import DEFAULT_LIMITS
from tara_studio.generator import generate_circle, generate_helix, generate_heart, generate_cube, generate_spline
from tara_studio.validator import validate_choreography
from tara_studio.exporter import export_kml, export_csv, export_json

def test_generate_shapes():
    shapes = [
        generate_circle(3, 10.0, 2.0),
        generate_helix(3, 10.0, 2.0),
        generate_heart(3, 10.0, 2.0),
        generate_cube(3, 10.0, 2.0),
        generate_spline(3, 10.0, 2.0)
    ]
    for choreo in shapes:
        assert "drones" in choreo
        assert len(choreo["drones"]) == 3
        assert len(choreo["drones"][0]["waypoints"]) > 0

def test_validator_passed():
    """A comfortably separated show passes at the real default thresholds.

    This used to call generate_circle with safe_dist_threshold=0.5 — a quarter
    of the design limit and a third of the limit the fleet actually aborts on.
    That made the assertion vacuous: it certified a show whose true minimum
    separation was 0.77 m. The threshold override is gone, so this now exercises
    the limits the system really enforces.
    """
    choreo = {
        "drones": [
            {
                "id": d_id,
                "waypoints": [
                    {"time": t * 0.5, "x": 0.0, "y": 6.0 * d_id, "z": -10.0, "yaw": 0.0}
                    for t in range(21)
                ],
            }
            for d_id in range(3)
        ]
    }
    res = validate_choreography(choreo)
    assert res["status"] == "PASSED"
    assert res["metrics"]["min_distance_m"] >= DEFAULT_LIMITS.design_separation_m
    assert len(res["violations"]["proximity"]) == 0


@pytest.mark.parametrize("shape", ["circle", "heart"])
def test_builtin_shapes_are_known_unsafe_during_landing(shape):
    """Documents a real defect rather than hiding it behind a loose threshold.

    In the landing phase the generators drive z = -height * (1 - ratio), so the
    vertical separation between stacked drones collapses to zero at touchdown
    while they are simultaneously converging horizontally onto their pads. The
    circle and heart shapes bottom out around 1.1 m — under the 1.5 m the fleet
    aborts on.

    When the landing phase is fixed, this test should start failing; replace it
    with an assertion that the shape PASSES.
    """
    fn = {"circle": generate_circle, "heart": generate_heart}[shape]
    res = validate_choreography(fn(num_drones=3, duration=30.0, rate=2.0))
    assert res["status"] == "FAILED"
    assert res["metrics"]["min_distance_m"] < DEFAULT_LIMITS.min_separation_m

def test_validator_failed_proximity():
    # Force two drones to occupy exact same coordinate
    choreo = {
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 5.0, "x": 5.0, "y": 5.0, "z": -2.0, "yaw": 0.0}
                ]
            },
            {
                "id": 1,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 5.0, "x": 5.0, "y": 5.0, "z": -2.0, "yaw": 0.0}
                ]
            }
        ]
    }
    res = validate_choreography(choreo, safe_dist_threshold=1.0)
    assert res["status"] == "FAILED"
    assert len(res["violations"]["proximity"]) > 0

def test_validator_failed_speed():
    # Force drone to fly at 50m/s
    choreo = {
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
                    {"time": 1.0, "x": 50.0, "y": 0.0, "z": -2.0, "yaw": 0.0}
                ]
            }
        ]
    }
    res = validate_choreography(choreo, max_velocity=4.0)
    assert res["status"] == "FAILED"
    assert len(res["violations"]["velocity"]) > 0

def test_kml_export():
    choreo = generate_circle(num_drones=2, duration=10.0, rate=2.0)
    kml = export_kml(choreo)
    assert "<kml" in kml
    assert "</kml>" in kml
    assert "Drone 00" in kml
    assert "relativeToGround" in kml

def test_json_csv_export():
    choreo = generate_circle(num_drones=2, duration=10.0, rate=2.0)
    json_out = export_json(choreo)
    csv_out = export_csv(choreo)
    
    assert "drones" in json_out
    assert "waypoints" in json_out
    assert "drone_id,time,x,y,z,yaw" in csv_out
