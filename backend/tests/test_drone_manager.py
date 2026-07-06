import pytest
import asyncio
from unittest.mock import AsyncMock, patch
from backend.drone_manager import DroneManager

@pytest.mark.asyncio
async def test_proximity_safety_breach():
    manager = DroneManager(ports=[])
    manager.trigger_rtl = AsyncMock()
    
    # Active armed drones too close (distance 1.0m, threshold 1.5m)
    manager.telemetry = {
        0: {
            "connected": True,
            "armed": True,
            "local_x": 0.0,
            "local_y": 0.0,
            "local_z": 2.0
        },
        1: {
            "connected": True,
            "armed": True,
            "local_x": 0.0,
            "local_y": 0.0,
            "local_z": 3.0
        }
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
    
    # Drones safe (distance 5.0m)
    manager.telemetry = {
        0: {
            "connected": True,
            "armed": True,
            "local_x": 0.0,
            "local_y": 0.0,
            "local_z": 2.0
        },
        1: {
            "connected": True,
            "armed": True,
            "local_x": 0.0,
            "local_y": 5.0,
            "local_z": 2.0
        }
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
    
    # Active armed drones too close (1.0m separation)
    manager.telemetry = {
        0: {
            "connected": True,
            "armed": True,
            "local_x": 0.0,
            "local_y": 0.0,
            "local_z": 2.0
        },
        1: {
            "connected": True,
            "armed": True,
            "local_x": 0.0,
            "local_y": 0.0,
            "local_z": 3.0
        }
    }
    
    task = asyncio.create_task(manager._proximity_monitor_loop(safety_limit=1.5))
    await asyncio.sleep(0.15)
    
    # Verify first breach was registered
    assert manager.proximity_breaches.get((0, 1)) is True
    
    # Drones separate to 5.0m
    manager.telemetry[1]["local_z"] = 7.0
    await asyncio.sleep(0.15)
    
    # Verify breach flag is reset to False
    assert manager.proximity_breaches.get((0, 1)) is False
    
    # Drones get close again (1.0m)
    manager.telemetry[1]["local_z"] = 3.0
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
