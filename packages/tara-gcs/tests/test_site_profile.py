"""Per-show site profile in the GCS: the launch-pad grid and the upload path follow the show's ``site`` key.

A 400 drone show needs a wider horizontal site than the 3 and 35 drone demos, but changing the global default
(30 m, 13 pad columns) would move every drone's pad and invalidate the SITL spawn layout and the locked demo
baselines. So the radius is a property of the show: ``standard`` (the default, and the only thing a show without a
``site`` key ever sees) is byte-for-byte the grid that existed before profiles, and a show opts in to ``scale400``.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from tara_core import MAX_FLEET_SIZE, SHOW_FORMAT_VERSION, launch_grid, limits_for_site
from tara_core.signing import TrustedKey, generate_private_key, sign_show
from tara_gcs import main as gcs_main
from tara_gcs.drone_manager import DroneManager, _pad_offset
from tara_gcs.show_auth import ShowTrustPolicy
from tara_gcs.trajectory_parser import show_site


def _show(site=None):
    body = {
        "version": SHOW_FORMAT_VERSION,
        "drones": [{"id": 0, "waypoints": [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0},
            {"time": 8.0, "x": 0.0, "y": 0.0, "z": -3.0, "yaw": 0.0},
        ]}],
    }
    if site is not None:
        body["site"] = site
    return json.dumps(body).encode()


# --- the pad grid -----------------------------------------------------------------------------------------------

def test_standard_pad_grid_is_exactly_what_it_was_before_profiles():
    """No `site` argument, "standard" and the core's own default grid are the same list, drone for drone."""
    reference = launch_grid(MAX_FLEET_SIZE)
    for drone_id in (0, 1, 12, 13, 14, 100, 399, MAX_FLEET_SIZE - 1):
        assert _pad_offset(drone_id) == reference[drone_id]
        assert _pad_offset(drone_id, "standard") == reference[drone_id]
    # 13 columns: drone 13 starts the second row.
    assert _pad_offset(13) == (2.2, 0.0)


def test_scale400_pad_grid_is_27_columns_wide():
    assert _pad_offset(26, "scale400") == (0.0, pytest.approx(26 * 2.2))
    assert _pad_offset(27, "scale400") == (pytest.approx(2.2), 0.0)
    # Drones 0..12 do not move; drone 13 and above do. This is why the grid cannot be a global default.
    assert [_pad_offset(i, "scale400") for i in range(13)] == [_pad_offset(i) for i in range(13)]
    assert _pad_offset(13, "scale400") != _pad_offset(13)


def test_site_profiles_differ_only_in_the_horizontal_radius():
    standard, wide = limits_for_site("standard"), limits_for_site("scale400")
    assert (standard.geofence_radius_m, wide.geofence_radius_m) == (30.0, 60.0)
    assert wide.max_altitude_m == standard.max_altitude_m == 120.0
    assert wide.min_separation_m == standard.min_separation_m
    assert limits_for_site(None) is limits_for_site("standard")


def test_an_unknown_site_is_refused_never_guessed():
    with pytest.raises(ValueError, match="unknown site profile"):
        limits_for_site("huge")
    with pytest.raises(ValueError, match="unknown site profile"):
        _pad_offset(0, "huge")


# --- reading the site from a show ------------------------------------------------------------------------------

def test_show_site_reads_the_json_key_and_defaults_to_standard():
    assert show_site(_show(), "show.json") == "standard"
    assert show_site(_show("scale400"), "show.json") == "scale400"
    assert show_site(b"time,x,y\n0,0,0\n", "show.csv") == "standard"
    assert show_site(b"{not json", "show.json") == "standard"  # the parser reports a malformed file itself
    with pytest.raises(ValueError):
        show_site(_show("huge"), "show.json")


# --- the manager -----------------------------------------------------------------------------------------------

def test_set_site_is_refused_while_a_drone_is_armed():
    manager = DroneManager(ports=[0, 1], backend="sim")
    manager.telemetry.setdefault(0, {})["armed"] = True
    with pytest.raises(ValueError, match="armed"):
        manager.set_site("scale400")
    assert manager.site == "standard"
    manager.telemetry[0]["armed"] = False
    manager.set_site("scale400")
    assert manager.site == "scale400"
    manager.set_site(None)
    assert manager.site == "standard"


@pytest.mark.asyncio
async def test_fleet_frame_telemetry_follows_the_sites_pads():
    """A drone sitting on its pad reports the pad's fleet-frame position, so the conversion must use the site's grid."""
    manager = DroneManager(ports=list(range(14)), backend="sim")
    try:
        await manager.connect_all()
        await asyncio.sleep(0.6)
        assert (manager.telemetry[13]["fleet_x"], manager.telemetry[13]["fleet_y"]) == pytest.approx(_pad_offset(13), abs=0.05)
        manager.set_site("scale400")
        await asyncio.sleep(0.6)
        assert (manager.telemetry[13]["fleet_x"], manager.telemetry[13]["fleet_y"]) == pytest.approx(
            _pad_offset(13, "scale400"), abs=0.05
        )
    finally:
        await manager.shutdown()


# --- the upload endpoint ---------------------------------------------------------------------------------------

@pytest.fixture
def gcs(monkeypatch):
    key = generate_private_key()
    trusted = TrustedKey(signer="Harsh", public_key=key.public_key())
    monkeypatch.setattr(gcs_main, "trust_policy", ShowTrustPolicy(trusted_keys={trusted.key_id: trusted}))
    monkeypatch.setattr(gcs_main, "drone_manager", DroneManager(ports=[0], backend="sim"))
    return TestClient(gcs_main.app), key


def _upload(client, key, content):
    files = {
        "file": ("show.json", content, "application/json"),
        "signature": ("show.json.sig.json", sign_show(content, "show.json", key), "application/json"),
    }
    return client.post("/api/upload-trajectory", files=files)


def test_upload_applies_the_shows_site_and_a_later_show_without_one_resets_it(gcs):
    client, key = gcs

    resp = _upload(client, key, _show("scale400"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["site"] == "scale400"
    assert gcs_main.drone_manager.site == "scale400"

    resp = _upload(client, key, _show())
    assert resp.status_code == 200, resp.text
    assert resp.json()["site"] == "standard"
    assert gcs_main.drone_manager.site == "standard"


def test_upload_of_an_unknown_site_is_a_400_and_changes_nothing(gcs):
    client, key = gcs
    assert _upload(client, key, _show("scale400")).status_code == 200
    kept = gcs_main.drone_manager.active_trajectory

    resp = _upload(client, key, _show("huge"))

    assert resp.status_code == 400
    assert "unknown site profile" in resp.text
    assert gcs_main.drone_manager.site == "scale400"
    assert gcs_main.drone_manager.active_trajectory is kept


def test_upload_while_armed_on_another_site_is_refused_and_keeps_the_active_show(gcs):
    client, key = gcs
    assert _upload(client, key, _show()).status_code == 200
    kept = gcs_main.drone_manager.active_trajectory
    gcs_main.drone_manager.telemetry.setdefault(0, {})["armed"] = True

    resp = _upload(client, key, _show("scale400"))

    assert resp.status_code == 400
    assert "armed" in resp.text
    assert gcs_main.drone_manager.site == "standard"
    assert gcs_main.drone_manager.active_trajectory is kept
