import pytest
from studio.generator import generate_circle, generate_helix, generate_heart, generate_cube, generate_spline
from studio.validator import validate_choreography
from studio.exporter import export_kml, export_csv, export_json

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
    choreo = generate_circle(num_drones=3, duration=15.0, rate=2.0)
    res = validate_choreography(
        choreo, 
        safe_dist_threshold=0.5,
        max_velocity=6.0,
        max_accel=60.0,
        max_yaw_rate=300.0
    )
    assert res["status"] == "PASSED"
    assert res["metrics"]["min_distance_m"] >= 0.5
    assert len(res["violations"]["proximity"]) == 0

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
