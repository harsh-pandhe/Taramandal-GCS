import pytest
import pandas as pd
import json
from backend.trajectory_parser import (
    _parse_json,
    _parse_csv,
    validate_trajectory_collisions,
    parse_trajectory_bytes
)
from backend.drone_manager import DroneManager

def test_parse_json_valid():
    valid_json = {
        "drones": [
            {
                "id": 0,
                "waypoints": [
                    {"time": 0.0, "x": 1.0, "y": 2.0, "z": -3.0, "yaw": 0.0},
                    {"time": 2.5, "x": 2.0, "y": 3.0, "z": -4.0, "yaw": 90.0}
                ]
            },
            {
                "id": 1,
                "waypoints": [
                    {"time": 0.0, "x": 10.0, "y": 20.0, "z": -5.0, "yaw": 0.0}
                ]
            }
        ]
    }
    content = json.dumps(valid_json)
    res = _parse_json(content)
    
    assert 0 in res
    assert 1 in res
    assert len(res[0]) == 2
    assert res[0][0]["time"] == 0.0
    assert res[0][1]["z"] == -4.0
    assert res[1][0]["x"] == 10.0

def test_parse_json_missing_id():
    invalid_json = {
        "drones": [
            {
                "waypoints": [{"time": 0.0, "x": 1.0, "y": 2.0, "z": -3.0}]
            }
        ]
    }
    content = json.dumps(invalid_json)
    with pytest.raises(ValueError, match="Drone entry is missing required 'id' key"):
        _parse_json(content)

def test_parse_json_invalid_id():
    invalid_json = {
        "drones": [
            {
                "id": "abc",
                "waypoints": [{"time": 0.0, "x": 1.0, "y": 2.0, "z": -3.0}]
            }
        ]
    }
    content = json.dumps(invalid_json)
    with pytest.raises(ValueError, match="Invalid drone 'id' value"):
        _parse_json(content)

def test_parse_json_out_of_range_id():
    invalid_json = {
        "drones": [
            {
                "id": 105,
                "waypoints": [{"time": 0.0, "x": 1.0, "y": 2.0, "z": -3.0}]
            }
        ]
    }
    content = json.dumps(invalid_json)
    with pytest.raises(ValueError, match="Drone ID out of range"):
        _parse_json(content)

def test_parse_csv_valid():
    valid_csv = """drone_id,time,x,y,z,yaw
0,0.0,1.0,2.0,-3.0,0.0
0,5.0,2.0,3.0,-4.0,90.0
1,0.0,10.0,20.0,-5.0,0.0
"""
    res = _parse_csv(valid_csv)
    assert 0 in res
    assert 1 in res
    assert len(res[0]) == 2
    assert res[0][1]["time"] == 5.0
    assert res[1][0]["y"] == 20.0

def test_parse_csv_too_many_rows():
    # Construct a CSV with 100,005 rows
    lines = ["drone_id,time,x,y,z,yaw"]
    for i in range(100005):
        lines.append(f"0,{i * 0.1},0.0,0.0,-2.0,0.0")
    large_csv = "\n".join(lines)
    
    with pytest.raises(ValueError, match="CSV exceeds maximum trajectory row limit"):
        _parse_csv(large_csv)

def test_parse_csv_invalid_id():
    invalid_csv = """drone_id,time,x,y,z,yaw
xyz,0.0,1.0,2.0,-3.0,0.0
"""
    with pytest.raises(ValueError, match="Invalid drone ID value"):
        _parse_csv(invalid_csv)

def test_validate_collisions_safe():
    # Two drones separated horizontally by 10 meters - safe (safety limit = 1.5m)
    traj = {
        0: [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
            {"time": 5.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}
        ],
        1: [
            {"time": 0.0, "x": 10.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
            {"time": 5.0, "x": 10.0, "y": 0.0, "z": -2.0, "yaw": 0.0}
        ]
    }
    errors = validate_trajectory_collisions(traj, safety_distance=1.5)
    assert len(errors) == 0

def test_validate_collisions_unsafe():
    # Two drones cross paths at time 2.5
    traj = {
        0: [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
            {"time": 5.0, "x": 5.0, "y": 5.0, "z": -2.0, "yaw": 0.0}
        ],
        1: [
            {"time": 0.0, "x": 5.0, "y": 5.0, "z": -2.0, "yaw": 0.0},
            {"time": 5.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0}
        ]
    }
    errors = validate_trajectory_collisions(traj, safety_distance=1.5)
    assert len(errors) > 0
    assert "Collision risk" in errors[0]

def test_drone_remapper():
    # Instantiate DroneManager without active MAVSDK systems (ports=[])
    manager = DroneManager(ports=[])
    
    # Set telemetry states manually
    # Drone 0 is healthy (connected, battery > 20, GPS sats >= 6)
    # Drone 1 is unhealthy (battery < 20)
    # Drone 2 is healthy
    manager.telemetry = {
        0: {"connected": True, "battery_percent": 90.0, "satellites": 8},
        1: {"connected": True, "battery_percent": 15.0, "satellites": 8},
        2: {"connected": True, "battery_percent": 85.0, "satellites": 7}
    }
    
    # Input trajectory contains slot keys (0 and 1)
    trajectory = {
        0: [{"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0}],
        1: [{"time": 0.0, "x": 5.0, "y": 5.0, "z": -2.0}]
    }
    
    remapped = manager.remap_trajectory_to_healthy_drones(trajectory)
    
    # Healthy drones: [0, 2]
    # Remapped output should assign slot 0 to Drone 0, slot 1 to Drone 2
    assert 0 in remapped
    assert 2 in remapped
    assert 1 not in remapped
    assert remapped[0][0]["x"] == 0.0
    assert remapped[2][0]["x"] == 5.0
