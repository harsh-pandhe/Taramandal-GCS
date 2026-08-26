import csv
import asyncio
import pytest
from tara_gcs.drone_manager import DroneManager


def _make_telemetry(fleet_x, fleet_y, fleet_z, battery_percent, flight_mode, connected):
    """Builds a telemetry entry using the same field names DroneManager's monitor loops populate."""
    return {
        "connected": connected,
        "armed": True,
        "fleet_x": fleet_x,
        "fleet_y": fleet_y,
        "fleet_z": fleet_z,
        "battery_percent": battery_percent,
        "flight_mode": flight_mode,
    }


@pytest.mark.asyncio
async def test_telemetry_recording_samples_all_drones():
    """Recording loop should append one row per connected drone per sample tick."""
    manager = DroneManager(ports=[])
    manager.TELEMETRY_SAMPLE_INTERVAL_S = 0.05  # fast tick for the test, doesn't touch the 10Hz control loops
    manager.telemetry = {
        0: _make_telemetry(0.0, 0.0, 2.0, 95.0, "OFFBOARD", True),
        1: _make_telemetry(0.0, 2.0, 2.7, 88.5, "OFFBOARD", True),
    }

    manager.start_telemetry_recording()
    await asyncio.sleep(0.22)  # ~4 sample ticks
    manager.stop_telemetry_recording()

    assert manager.is_recording_telemetry is False
    assert len(manager.telemetry_log) >= 4  # at least 2 ticks x 2 drones

    sample = manager.telemetry_log[0]
    assert sample["drone_id"] in (0, 1)
    assert sample["x"] == manager.telemetry[sample["drone_id"]]["fleet_x"]
    assert sample["battery_pct"] in (95.0, 88.5)
    assert sample["mode"] == "OFFBOARD"
    assert sample["connected"] is True


@pytest.mark.asyncio
async def test_telemetry_recording_restarts_clear_previous_log():
    """Re-calling start_telemetry_recording() should discard samples from a prior show."""
    manager = DroneManager(ports=[])
    manager.telemetry_log = [{"timestamp": 0.0, "drone_id": 0, "x": 0.0, "y": 0.0, "z": 0.0,
                               "battery_pct": 50.0, "mode": "HOLD", "connected": True}]
    manager.telemetry = {0: _make_telemetry(0.0, 0.0, 2.0, 90.0, "OFFBOARD", True)}

    manager.start_telemetry_recording()
    manager.stop_telemetry_recording()

    assert manager.telemetry_log == []


def test_export_telemetry_csv_writes_expected_columns(tmp_path):
    """Verifies the CSV export uses the field names required by issue #8's acceptance criteria."""
    manager = DroneManager(ports=[])
    manager.telemetry_log = [
        {"timestamp": 1000.0, "drone_id": 0, "x": 1.5, "y": -0.5, "z": 2.0,
         "battery_pct": 91.2, "mode": "OFFBOARD", "connected": True},
        {"timestamp": 1000.5, "drone_id": 1, "x": 0.0, "y": 2.0, "z": 2.7,
         "battery_pct": 88.0, "mode": "HOLD", "connected": False},
    ]

    out_file = tmp_path / "telemetry_export.csv"
    row_count = manager.export_telemetry_csv(str(out_file))

    assert row_count == 2
    assert out_file.exists()

    with open(out_file, newline="") as f:
        rows = list(csv.DictReader(f))

    assert rows[0].keys() == {
        "timestamp", "drone_id", "x", "y", "z", "battery_pct", "mode", "connected"
    }
    assert rows[0]["drone_id"] == "0"
    assert rows[0]["x"] == "1.5"
    assert rows[0]["battery_pct"] == "91.2"
    assert rows[0]["mode"] == "OFFBOARD"
    assert rows[0]["connected"] == "True"
    assert rows[1]["connected"] == "False"


@pytest.mark.asyncio
async def test_record_then_export_end_to_end(tmp_path):
    """Simulates a full show: record telemetry against sim-style telemetry dicts, then export."""
    manager = DroneManager(ports=[])
    manager.TELEMETRY_SAMPLE_INTERVAL_S = 0.05
    manager.telemetry = {
        0: _make_telemetry(0.0, 0.0, 2.0, 95.0, "OFFBOARD", True),
    }

    manager.start_telemetry_recording()
    await asyncio.sleep(0.12)
    manager.stop_telemetry_recording()

    out_file = tmp_path / "show_export.csv"
    row_count = manager.export_telemetry_csv(str(out_file))

    assert row_count == len(manager.telemetry_log)
    assert row_count >= 2

    with open(out_file, newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == row_count
    assert all(row["drone_id"] == "0" for row in rows)
