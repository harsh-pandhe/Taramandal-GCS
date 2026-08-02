import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from backend.drone_manager import DroneManager, _pad_offset

def _make_telemetry(local_x, local_y, local_z, drone_id):
    """Builds a telemetry entry with fleet_x/y/z derived the same way _monitor_position does."""
    pad_x_off, pad_y_off = _pad_offset(drone_id)
    return {
        "connected": True,
        "armed": True,
        "local_x": local_x,
        "local_y": local_y,
        "local_z": local_z,
        "fleet_x": local_x + pad_x_off,
        "fleet_y": local_y + pad_y_off,
        "fleet_z": local_z,
    }

@pytest.mark.asyncio
async def test_proximity_safety_breach():
    manager = DroneManager(ports=[])
    manager.trigger_rtl = AsyncMock()

    # Both drones armed at their own pad-local origin. In the fleet frame their
    # pads are 2.0m apart on Y (LAUNCH_PAD_SPACING_Y_M), which is inside the 1.5m
    # safety limit — i.e. they've drifted onto/near each other's pad.
    manager.telemetry = {
        0: _make_telemetry(local_x=0.0, local_y=0.0, local_z=2.0, drone_id=0),
        1: _make_telemetry(local_x=0.0, local_y=-1.0, local_z=2.0, drone_id=1),
    }

    task = asyncio.create_task(manager._proximity_monitor_loop(safety_limit=1.5))
    await asyncio.sleep(0.15)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    manager.trigger_rtl.assert_called_once()
    assert manager.proximity_breaches.get((0, 1)) is True

@pytest.mark.asyncio
async def test_proximity_safety_safe():
    manager = DroneManager(ports=[])
    manager.trigger_rtl = AsyncMock()

    # Drones each near their own pad — fleet-frame separation is the full
    # 2.0m pad spacing plus a bit more, well outside the 1.5m safety limit.
    manager.telemetry = {
        0: _make_telemetry(local_x=0.0, local_y=0.0, local_z=2.0, drone_id=0),
        1: _make_telemetry(local_x=0.0, local_y=0.0, local_z=2.0, drone_id=1),
    }

    task = asyncio.create_task(manager._proximity_monitor_loop(safety_limit=1.5))
    await asyncio.sleep(0.15)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    manager.trigger_rtl.assert_not_called()
    assert manager.proximity_breaches.get((0, 1), False) is False

@pytest.mark.asyncio
async def test_proximity_safety_separation():
    manager = DroneManager(ports=[])
    manager.trigger_rtl = AsyncMock()

    # Start breached: both drones drift to the same fleet-frame position.
    manager.telemetry = {
        0: _make_telemetry(local_x=0.0, local_y=0.0, local_z=2.0, drone_id=0),
        1: _make_telemetry(local_x=0.0, local_y=-2.0, local_z=2.0, drone_id=1),
    }

    task = asyncio.create_task(manager._proximity_monitor_loop(safety_limit=1.5))
    await asyncio.sleep(0.15)

    # Verify first breach was registered
    assert manager.proximity_breaches.get((0, 1)) is True

    # Drone 1 climbs well clear (5m altitude separation in fleet frame)
    manager.telemetry[1]["fleet_z"] = 7.0
    await asyncio.sleep(0.15)

    # Verify breach flag is reset to False
    assert manager.proximity_breaches.get((0, 1)) is False

    # Drones get close again
    manager.telemetry[1]["fleet_z"] = 2.5
    await asyncio.sleep(0.15)

    # Verify breach is registered again
    assert manager.proximity_breaches.get((0, 1)) is True

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

@pytest.mark.asyncio
async def test_geofence_safety_breach():
    manager = DroneManager(ports=[])
    manager.trigger_rtl = AsyncMock()
    manager.geofence_radius = 10.0
    
    # Drone outside geofence (dist = 12.0m)
    manager.telemetry = {
        0: {
            "connected": True,
            "armed": True,
            "local_x": 0.0,
            "local_y": 12.0,
            "local_z": 2.0
        }
    }
    
    task = asyncio.create_task(manager._geofence_monitor_loop())
    await asyncio.sleep(0.15)
    
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
        
    manager.trigger_rtl.assert_called_once()
    assert manager.geofence_breaches.get(0) is True

@pytest.mark.asyncio
async def test_geofence_safety_safe():
    manager = DroneManager(ports=[])
    manager.trigger_rtl = AsyncMock()
    manager.geofence_radius = 15.0
    
    # Drone inside geofence (dist = 5.0m)
    manager.telemetry = {
        0: {
            "connected": True,
            "armed": True,
            "local_x": 3.0,
            "local_y": 4.0,
            "local_z": 2.0
        }
    }
    
    task = asyncio.create_task(manager._geofence_monitor_loop())
    await asyncio.sleep(0.15)
    
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
        
    manager.trigger_rtl.assert_not_called()
    assert manager.geofence_breaches.get(0, False) is False

@pytest.mark.asyncio
async def test_telemetry_fields_exist():
    manager = DroneManager(ports=[])
    with patch('backend.drone_manager.System') as mock_system:
        mock_instance = mock_system.return_value
        mock_instance.connect = AsyncMock()
        
        await manager._connect_drone(drone_id=0, port=14540)
        
        # Clean up tasks immediately to prevent leaks
        await manager.shutdown()
        
        assert 0 in manager.telemetry
        assert manager.telemetry[0]["gps_fix_type"] == "NO_GPS"
        assert manager.telemetry[0]["comm_latency_ms"] == 0.0

@pytest.mark.asyncio
async def test_verify_launch_geometry():
    manager = DroneManager(ports=[])
    # First waypoint (t=0) is at fleet-frame (0.0, 0.0, height 2.0) — drone 0's
    # own pad in a single-drone trajectory (pad offset for id 0 is (0,0)).
    trajectory_data = {
        0: [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": -2.0, "yaw": 0.0},
            {"time": 1.0, "x": 1.0, "y": 0.0, "z": -2.0, "yaw": 0.0}
        ]
    }

    # 1. Connected and placed correctly (within 0.5m in the fleet frame)
    manager.telemetry = {
        0: {
            "connected": True,
            "fleet_x": 0.1,
            "fleet_y": 0.0,
            "fleet_z": 2.0,
        }
    }
    manager.remap_trajectory_to_healthy_drones = lambda td: td

    report = manager.verify_launch_geometry(trajectory_data, tolerance_m=0.5)
    assert report["all_passed"] is True
    assert report["drones"]["0"]["status"] == "PASSED"
    assert report["drones"]["0"]["distance_error_m"] == 0.1

    # 2. Connected but placed on the wrong pad (2.0m off in the fleet frame) —
    # this is exactly the "wrong pad/slot" case the check exists to catch.
    manager.telemetry[0]["fleet_x"] = 2.0
    report = manager.verify_launch_geometry(trajectory_data, tolerance_m=0.5)
    assert report["all_passed"] is False
    assert report["drones"]["0"]["status"] == "FAILED"
    assert report["drones"]["0"]["distance_error_m"] == 2.0

@pytest.mark.asyncio
async def test_verify_launch_geometry_rejects_wrong_pad_assignment():
    """Proves the check is real: a drone armed on someone else's pad must fail."""
    manager = DroneManager(ports=[])
    # Trajectory slot 1 expects its drone at fleet Y=2.0 (its own pad, per
    # LAUNCH_PAD_SPACING_Y_M), but telemetry shows it still sitting at fleet
    # Y=0.0 (drone 0's pad) — a genuine wrong-slot/wrong-pad scenario.
    trajectory_data = {
        1: [
            {"time": 0.0, "x": 0.0, "y": 2.0, "z": -2.7, "yaw": 0.0},
        ]
    }
    manager.telemetry = {
        1: {
            "connected": True,
            "fleet_x": 0.0,
            "fleet_y": 0.0,
            "fleet_z": 2.7,
        }
    }
    manager.remap_trajectory_to_healthy_drones = lambda td: td

    report = manager.verify_launch_geometry(trajectory_data, tolerance_m=0.5)
    assert report["all_passed"] is False
    assert report["drones"]["1"]["status"] == "FAILED"
    assert report["drones"]["1"]["distance_error_m"] == 2.0

@pytest.mark.asyncio
async def test_launch_one_is_idempotent_for_already_armed_drone():
    """Re-invoking launch on an already-armed drone must be a no-op (never re-commands it to origin)."""
    manager = DroneManager(ports=[])
    mock_drone = AsyncMock()
    manager.drones = {0: mock_drone}
    manager.telemetry = {
        0: {"connected": True, "armed": True}
    }

    await manager._launch_one(drone_id=0)

    mock_drone.action.arm.assert_not_called()
    mock_drone.offboard.set_position_ned.assert_not_called()
    mock_drone.offboard.start.assert_not_called()

@pytest.mark.asyncio
async def test_launch_one_skips_disconnected_drone():
    manager = DroneManager(ports=[])
    mock_drone = AsyncMock()
    manager.drones = {0: mock_drone}
    manager.telemetry = {
        0: {"connected": False, "armed": False}
    }

    await manager._launch_one(drone_id=0)

    mock_drone.action.arm.assert_not_called()
