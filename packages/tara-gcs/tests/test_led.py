"""LED colour published with the planned position, so the dashboard can paint a show the way Studio does."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from tara_gcs.drone_manager import DroneManager
from tara_gcs.led import led_at


def wp(t, r=None, g=None, b=None, x=0.0):
    w = {"time": t, "x": x, "y": 0.0, "z": -5.0, "yaw": 0.0}
    for name, value in (("r", r), ("g", g), ("b", b)):
        if value is not None:
            w[name] = value
    return w


class TestLedAt:
    def test_no_waypoints_or_no_colour_data_gives_none(self):
        assert led_at([], 1.0) is None
        assert led_at([wp(0.0), wp(5.0)], 1.0) is None  # a show without light data

    def test_blends_linearly_between_waypoints(self):
        wps = [wp(0.0, 0, 0, 0), wp(10.0, 100, 200, 250)]
        assert led_at(wps, 5.0) == pytest.approx((50.0, 100.0, 125.0))
        assert led_at(wps, 2.0) == pytest.approx((20.0, 40.0, 50.0))

    def test_holds_the_ends_outside_the_time_span(self):
        wps = [wp(1.0, 10, 20, 30), wp(2.0, 40, 50, 60)]
        assert led_at(wps, -5.0) == (10.0, 20.0, 30.0)
        assert led_at(wps, 99.0) == (40.0, 50.0, 60.0)

    def test_dark_waypoints_read_as_off(self):
        wps = [wp(0.0, 0, 0, 0), wp(1.0, 0, 0, 0), wp(2.0, 255, 120, 0)]
        assert led_at(wps, 0.5) == (0.0, 0.0, 0.0)

    def test_a_flash_is_not_smoothed_away(self):
        # Studio bakes colour at 10 Hz: an instant on/off pair must stay sharp at the sample times.
        wps = [wp(0.0, 0, 0, 0), wp(0.1, 255, 255, 255), wp(0.2, 0, 0, 0)]
        assert led_at(wps, 0.1) == (255.0, 255.0, 255.0)
        assert led_at(wps, 0.2) == (0.0, 0.0, 0.0)

    def test_a_channel_missing_mid_show_counts_as_dark_for_that_channel(self):
        wps = [wp(0.0, 10, 10, 10), wp(2.0, r=200)]
        assert led_at(wps, 2.0) == (200.0, 0.0, 0.0)

    def test_duplicate_timestamps_do_not_divide_by_zero(self):
        wps = [wp(0.0, 0, 0, 0), wp(1.0, 50, 50, 50), wp(1.0, 200, 200, 200), wp(2.0, 200, 200, 200)]
        assert led_at(wps, 1.0) is not None


def _manager(n=2):
    manager = DroneManager(ports=[])
    manager.drones = {i: AsyncMock() for i in range(n)}
    manager.telemetry = {
        i: {"connected": True, "armed": True, "battery_percent": 90.0, "satellites": 10,
            "fleet_x": 0.0, "fleet_y": 0.0, "fleet_z": 5.0, "planned_valid": False, "aborted": None,
            "led_r": 0.0, "led_g": 0.0, "led_b": 0.0, "led_valid": False}
        for i in range(n)
    }
    return manager


async def test_playback_publishes_the_commanded_colour_next_to_the_planned_position():
    manager = _manager(2)
    coloured = [wp(0.0, 255, 0, 0), wp(20.0, 255, 0, 0)]
    plain = [wp(0.0), wp(20.0)]  # drone 1's show has no light data
    manager.is_running_trajectory = True
    task = asyncio.create_task(manager._run_trajectory_loop({0: coloured, 1: plain}))
    await asyncio.sleep(0.3)
    assert manager.telemetry[0]["led_valid"] is True
    assert (manager.telemetry[0]["led_r"], manager.telemetry[0]["led_g"], manager.telemetry[0]["led_b"]) == (255.0, 0.0, 0.0)
    assert manager.telemetry[1]["led_valid"] is False  # falls back to the dashboard's own colours
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    # once playback stops there is no commanded colour any more
    assert manager.telemetry[0]["led_valid"] is False


async def test_an_aborted_drone_stops_publishing_a_colour():
    manager = _manager(1)
    manager.telemetry[0]["led_valid"] = True
    manager.drones[0].action.return_to_launch = AsyncMock()
    manager.drones[0].offboard.stop = AsyncMock()
    manager.telemetry[0]["fleet_x"], manager.telemetry[0]["fleet_z"] = 20.0, 15.0
    await manager.abort_drone(0, "rtl")
    assert manager.telemetry[0]["led_valid"] is False
