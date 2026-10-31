"""DroneManager.inject_fault / POST /api/sim/inject-fault (issue #63).

Every fault type but motor_failure is a direct tara_sim.FleetState edit that
every existing monitor (preflight's GNSS gate, the dashboard's telemetry-fed
HUD) already reads from — nothing new to make those react. motor_failure
instead drives the real single-drone abort sequence (DroneManager.abort_drone,
already covered by its own tests) rather than faking a sensor reading, so
this file checks it was actually called with the right arguments rather than
re-testing abort_drone's own internals.
"""

from __future__ import annotations

import asyncio

import pytest

from tara_gcs.drone_manager import DroneManager, _pad_offset


async def _connected_manager(n: int = 3, **kwargs) -> DroneManager:
    manager = DroneManager(ports=list(range(n)), backend="sim", **kwargs)
    await manager.connect_all()
    return manager


class TestRtkLoss:
    @pytest.mark.asyncio
    async def test_sets_the_fix_type_in_telemetry(self):
        manager = await _connected_manager()
        try:
            result = await manager.inject_fault(1, "rtk_loss", {"fix_type": "RTK_FLOAT"})
            assert result["fault_type"] == "rtk_loss"
            assert manager._sim_fleet.state.gps_fix_type[1] == "RTK_FLOAT"
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_rejects_an_unknown_fix_type(self):
        manager = await _connected_manager()
        try:
            with pytest.raises(ValueError):
                await manager.inject_fault(0, "rtk_loss", {"fix_type": "GPS_IS_GREAT"})
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_degrading_sequence_from_the_issue_all_apply_cleanly(self):
        """The issue's own example: Fixed -> Float -> 3D Fix -> No GPS."""
        manager = await _connected_manager()
        try:
            for fix_type in ("RTK_FIXED", "RTK_FLOAT", "FIX_3D", "NO_GPS"):
                await manager.inject_fault(0, "rtk_loss", {"fix_type": fix_type})
                assert manager._sim_fleet.state.gps_fix_type[0] == fix_type
        finally:
            await manager.shutdown()


class TestBatteryDrop:
    @pytest.mark.asyncio
    async def test_sets_the_percentage(self):
        manager = await _connected_manager()
        try:
            await manager.inject_fault(0, "battery_drop", {"pct": 8.0})
            assert manager._sim_fleet.state.battery_pct[0] == pytest.approx(8.0)
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_rejects_a_missing_pct(self):
        manager = await _connected_manager()
        try:
            with pytest.raises(ValueError):
                await manager.inject_fault(0, "battery_drop", {})
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_rejects_an_out_of_range_pct(self):
        manager = await _connected_manager()
        try:
            with pytest.raises(ValueError):
                await manager.inject_fault(0, "battery_drop", {"pct": 150.0})
        finally:
            await manager.shutdown()


class TestDeviation:
    @pytest.mark.asyncio
    async def test_offsets_the_drone_s_position(self):
        manager = await _connected_manager()
        try:
            before = manager._sim_fleet.state.position[0].copy()
            await manager.inject_fault(0, "deviation", {"offset_m": [1.5, 0.0, 0.0]})
            after = manager._sim_fleet.state.position[0]
            assert after[0] == pytest.approx(before[0] + 1.5)
            assert after[1] == pytest.approx(before[1])
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_rejects_a_malformed_offset(self):
        manager = await _connected_manager()
        try:
            with pytest.raises(ValueError):
                await manager.inject_fault(0, "deviation", {"offset_m": [1.5, 0.0]})  # only 2
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_triggers_the_real_proximity_monitor_when_it_closes_the_gap(self):
        """Acceptance criterion: "triggers GCS proximity warning" - exercised
        against the actual proximity_breaches state the live 10Hz monitor
        loop maintains (started by connect_all), by injecting a deviation
        that puts an armed drone on top of its neighbour's pad - well inside
        the design separation no matter how far apart the pads start -
        rather than just checking tara_sim's position array directly.

        Polls instead of a fixed sleep: the monitor loop and the bulk
        telemetry loop it reads from are two independent asyncio tasks, so a
        fixed delay would be guessing at their combined latency instead of
        just waiting for the actual condition.
        """
        manager = await _connected_manager(n=2)
        try:
            await asyncio.sleep(0.5)  # let the connection monitors report in
            x0, y0 = _pad_offset(0)
            x1, y1 = _pad_offset(1)
            show = {
                0: [{"time": 0.0, "x": x0, "y": y0, "z": 0.0}, {"time": 1.0, "x": x0, "y": y0, "z": -2.0}],
                1: [{"time": 0.0, "x": x1, "y": y1, "z": 0.0}, {"time": 1.0, "x": x1, "y": y1, "z": -2.0}],
            }
            await manager.upload_show(show)
            await manager.launch_sequence()

            for _ in range(50):  # up to ~2.5s for both drones to report armed
                await asyncio.sleep(0.05)
                if all(manager.telemetry[i].get("armed") for i in (0, 1)):
                    break

            await manager.inject_fault(0, "deviation", {"offset_m": [x1 - x0, y1 - y0, 0.0]})

            breached = False
            for _ in range(50):  # up to ~2.5s for the 10Hz monitor to catch it
                await asyncio.sleep(0.05)
                if manager.proximity_breaches.get((0, 1)) or manager.proximity_breaches.get((1, 0)):
                    breached = True
                    break
            assert breached
        finally:
            await manager.shutdown()


class TestMotorFailure:
    @pytest.mark.asyncio
    async def test_drives_the_real_abort_sequence_with_land_mode(self):
        manager = await _connected_manager()
        try:
            result = await manager.inject_fault(0, "motor_failure", {})
            assert result["mode"] == "land"
            assert result["drone_id"] == 0
            assert manager.telemetry[0]["aborted"]["type"] == "land"
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_ignores_params_entirely(self):
        """motor_failure never reads params - passing garbage there must not
        raise, since there is nothing for it to validate."""
        manager = await _connected_manager()
        try:
            result = await manager.inject_fault(0, "motor_failure", {"nonsense": True})
            assert result["drone_id"] == 0
        finally:
            await manager.shutdown()


class TestGuardrails:
    @pytest.mark.asyncio
    async def test_unknown_fault_type_rejected(self):
        manager = await _connected_manager()
        try:
            with pytest.raises(ValueError):
                await manager.inject_fault(0, "wing_fell_off", {})
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_unknown_drone_id_rejected(self):
        manager = await _connected_manager()
        try:
            with pytest.raises(KeyError):
                await manager.inject_fault(99, "battery_drop", {"pct": 10.0})
        finally:
            await manager.shutdown()

    @pytest.mark.asyncio
    async def test_px4_backend_rejected(self):
        manager = DroneManager(ports=[14540], backend="px4")
        with pytest.raises(ValueError):
            await manager.inject_fault(0, "battery_drop", {"pct": 10.0})

    @pytest.mark.asyncio
    async def test_sim_backend_before_connect_all_rejected(self):
        manager = DroneManager(ports=[0], backend="sim")
        with pytest.raises(ValueError):
            await manager.inject_fault(0, "battery_drop", {"pct": 10.0})
