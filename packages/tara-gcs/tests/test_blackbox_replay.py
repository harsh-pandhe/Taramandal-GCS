"""The replay reader and its HTTP routes (issue #65, replay slice).

A session is recorded with the real :class:`BlackboxRecorder` on a fake clock,
then read back through :mod:`tara_gcs.blackbox_replay` and through the real
FastAPI routes (``TestClient`` with the conftest logged-in operator).
"""

import gzip
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from tara_gcs import blackbox_replay as br
from tara_gcs import main as gcs_main
from tara_gcs.blackbox import BlackboxRecorder
from tests.test_blackbox import FakeClock, _fleet, _tel  # noqa: F401  (shared helpers)


@pytest.fixture
def clock():
    return FakeClock()


def _record(directory, clock, *, ticks=100, drones=4, close=True, events=()):
    os.makedirs(directory, exist_ok=True)
    rec = BlackboxRecorder.new_session(str(directory), clock=clock, wall_clock=lambda: 1_700_000_000.0,
                                       fsync_interval_s=None, keyframe_interval_s=2.0)
    for k in range(ticks):
        fleet = _fleet(drones, k)
        if k == 30:  # one drone drops its RF reading / goes unarmed for a while
            fleet[1] = _tel(1, k, armed=False, flight_mode="HOLD", gps_fix_type="NO_GPS")
        assert rec.record_tick(fleet, rf_drop_rate={i: 0.01 * i for i in range(drones)} if k else None)
        for kind, at, fields in events:
            if at == k:
                rec.record_event(kind, **fields)
        clock.advance()
    if close:
        rec.close()
    return rec


def test_list_sessions_is_newest_first_and_skips_junk(tmp_path, clock):
    d = tmp_path / "bb"
    a = _record(d, clock, ticks=5)
    os.utime(a.path, (1000, 1000))
    b = _record(d, clock, ticks=5)
    (d / "notes.txt").write_text("x")
    (d / "blackbox_bogus.jsonl.gz").write_bytes(gzip.compress(b'{"type": "event"}\n'))
    (d / "blackbox_corrupt.jsonl.gz").write_bytes(b"not gzip")
    rows = br.list_sessions(str(d), live_path=b.path)
    assert [r["id"] for r in rows] == [os.path.basename(b.path)[:-9], os.path.basename(a.path)[:-9]]
    assert rows[0]["live"] and not rows[1]["live"]
    assert rows[0]["started"].startswith("2023-11-14")


def test_list_sessions_missing_directory_is_empty(tmp_path):
    assert br.list_sessions(str(tmp_path / "nope")) == []


def test_summary_and_values_are_in_real_units(tmp_path, clock):
    rec = _record(tmp_path / "bb", clock, events=[("command", 10, {"command": "arm", "operator": "o", "trigger": "api"})])
    sid = os.path.basename(rec.path)[: -len(br.SESSION_SUFFIX)]
    s = br.ReplaySession.load(rec.path, sid)
    summary = s.summary()
    assert summary["ticks"] == 100 and summary["drone_ids"] == [0, 1, 2, 3]
    assert summary["event_kinds"] == {"command": 1} and not summary["truncated"]
    assert summary["duration_ms"] == 9900 and summary["flag_bits"]["armed"] == 1
    out = s.frames(from_ms=500, to_ms=500)
    assert out["count"] == 1
    f = out["frames"][0]
    assert f["t_ms"] == 500 and f["ids"] == [0, 1, 2, 3]
    # _tel: fleet_x = i + 0.05k, battery 100 - 0.01k, z = 10 + 0.01k (k = 5 here)
    assert f["x_m"] == [0.25, 1.25, 2.25, 3.25]
    assert f["z_m"][0] == pytest.approx(10.05)
    assert f["battery_percent"][0] == _tel(0, 5)["battery_percent"]  # quantised to 0.1 %
    assert f["mode"][0] == "OFFBOARD" and f["gps_fix"][0] == "FIX_3D"
    assert f["rf_drop_rate"] == [0.0, 0.01, 0.02, 0.03]
    assert f["flags"][0] & (1 << 1)  # armed


def test_missing_rf_reading_is_null_not_minus_one(tmp_path, clock):
    rec = _record(tmp_path / "bb", clock, ticks=3)
    s = br.ReplaySession.load(rec.path, "x")
    first = s.frames(from_ms=0, to_ms=0)["frames"][0]
    assert first["rf_drop_rate"] == [None] * 4  # k == 0 was recorded without an RF model


def test_per_drone_change_mid_session_decodes(tmp_path, clock):
    rec = _record(tmp_path / "bb", clock)
    s = br.ReplaySession.load(rec.path, "x")
    f = s.frames(from_ms=3000, to_ms=3000, drones=[1])["frames"][0]
    assert f["ids"] == [1] and f["mode"] == ["HOLD"] and f["gps_fix"] == ["NO_GPS"]
    assert not f["flags"][0] & (1 << 1)  # unarmed


def test_window_step_stride_and_paging(tmp_path, clock):
    rec = _record(tmp_path / "bb", clock, ticks=200)
    s = br.ReplaySession.load(rec.path, "x")
    all_ticks = s.frames()
    assert all_ticks["count"] == 200 and all_ticks["stride"] == 1
    stepped = s.frames(step_ms=1000)
    assert [f["t_ms"] for f in stepped["frames"]][:3] == [0, 1000, 2000] and stepped["count"] == 20
    capped = s.frames(max_frames=50)
    assert capped["count"] <= 50 and capped["stride"] == 4
    assert capped["frames"][1]["t_ms"] - capped["frames"][0]["t_ms"] == 400
    window = s.frames(from_ms=5000, to_ms=5900)
    assert [f["t_ms"] for f in window["frames"]] == list(range(5000, 6000, 100))
    assert s.frames(from_ms=10**9)["frames"] == []


def test_event_list_filters_and_limits(tmp_path, clock):
    ev = [("proximity_breach", 20, {"drones": [0, 1], "distance_m": 1.4}),
          ("command", 25, {"command": "land", "operator": "o", "trigger": "api"}),
          ("geofence_hard_breach", 40, {"drone": 2, "distance_m": 60.0, "limit_m": 50.0})]
    rec = _record(tmp_path / "bb", clock, events=ev)
    s = br.ReplaySession.load(rec.path, "x")
    assert [e["kind"] for e in s.event_list()["events"]] == ["proximity_breach", "command", "geofence_hard_breach"]
    safety = s.event_list(br.SAFETY_EVENT_KINDS)
    assert [e["kind"] for e in safety["events"]] == ["proximity_breach", "geofence_hard_breach"]
    limited = s.event_list(None, limit=1)
    assert limited["total"] == 3 and limited["limited"] and len(limited["events"]) == 1


def test_truncated_and_still_open_sessions_are_readable(tmp_path, clock):
    rec = _record(tmp_path / "bb", clock, close=False, events=[("proximity_breach", 50, {"drones": [0, 1], "distance_m": 1.0})])
    rec.flush()
    live = br.ReplaySession.load(rec.path, "x")  # no gzip trailer yet
    assert live.truncated and live.summary()["events"] == 1 and len(live.ticks) > 0
    rec.close()
    assert not br.ReplaySession.load(rec.path, "x").truncated


def test_cut_off_file_returns_its_prefix(tmp_path, clock):
    rec = _record(tmp_path / "bb", clock, ticks=300)
    cut = tmp_path / "bb" / "blackbox_cut.jsonl.gz"
    data = open(rec.path, "rb").read()
    cut.write_bytes(data[: len(data) // 2])
    s = br.ReplaySession.load(str(cut), "blackbox_cut")
    assert s.truncated and 0 < len(s.ticks) < 300


def test_session_id_cannot_leave_the_directory(tmp_path, clock):
    d = tmp_path / "bb"
    _record(d, clock, ticks=3)
    secret = tmp_path / "secret.jsonl.gz"
    shutil.copy(next(d.glob("blackbox_*")), secret)
    for bad in ("../secret", "..%2Fsecret", "blackbox_/../../secret", "/etc/passwd", "blackbox_x.y", "", "blackbox_" + "a" * 200):
        with pytest.raises(br.SessionNotFound):
            br.session_path(str(d), bad)
    # a symlink named like a session that points outside the directory is refused too
    link = d / "blackbox_link.jsonl.gz"
    link.symlink_to(secret)
    with pytest.raises(br.SessionNotFound):
        br.session_path(str(d), "blackbox_link")


def test_store_reuses_unchanged_file_and_rereads_a_grown_one(tmp_path, clock):
    d = tmp_path / "bb"
    rec = _record(d, clock, ticks=10, close=False)
    rec.flush()
    sid = os.path.basename(rec.path)[: -len(br.SESSION_SUFFIX)]
    store = br.ReplayStore(str(d))
    first = store.get(sid)
    assert store.get(sid) is first
    for k in range(10, 40):
        rec.record_tick(_fleet(4, k))
        clock.advance()
    rec.flush()
    second = store.get(sid)
    assert second is not first and len(second.ticks) > len(first.ticks)
    rec.close()


# ---------------------------------------------------------------- HTTP routes


@pytest.fixture
def api(tmp_path, clock, monkeypatch):
    d = tmp_path / "blackbox"  # the conftest fixture points GCS_BLACKBOX_DIR here
    rec = _record(d, clock, ticks=120, events=[
        ("proximity_breach", 20, {"drones": [0, 1], "distance_m": 1.4}),
        ("command", 25, {"command": "land", "operator": "o", "trigger": "api"}),
    ])
    monkeypatch.setattr(gcs_main, "blackbox_service", None)
    monkeypatch.setattr(gcs_main, "_replay_store", None)
    with TestClient(gcs_main.app) as client:
        client.sid = os.path.basename(rec.path)[: -len(br.SESSION_SUFFIX)]
        yield client


def test_routes_list_summarise_and_serve_frames(api):
    rows = api.get("/api/blackbox/sessions").json()["sessions"]
    assert api.sid in [r["id"] for r in rows]
    s = api.get(f"/api/blackbox/sessions/{api.sid}").json()
    assert s["ticks"] == 120 and s["events"] == 2 and s["drone_ids"] == [0, 1, 2, 3]
    ev = api.get(f"/api/blackbox/sessions/{api.sid}/events", params={"safety_only": True}).json()
    assert [e["kind"] for e in ev["events"]] == ["proximity_breach"]
    ev2 = api.get(f"/api/blackbox/sessions/{api.sid}/events", params={"kinds": "command,proximity_breach"}).json()
    assert ev2["total"] == 2
    fr = api.get(f"/api/blackbox/sessions/{api.sid}/frames", params={"from_ms": 1000, "to_ms": 1200, "drones": "0,2"}).json()
    assert fr["count"] == 3 and fr["frames"][0]["ids"] == [0, 2]


def test_routes_reject_bad_input(api):
    assert api.get("/api/blackbox/sessions/blackbox_nope").status_code == 404
    assert api.get("/api/blackbox/sessions/..%2Fsecret").status_code in (404, 422)
    assert api.get(f"/api/blackbox/sessions/{api.sid}/frames", params={"drones": "a,b"}).status_code == 422
    assert api.get(f"/api/blackbox/sessions/{api.sid}/frames", params={"step_ms": -5}).status_code == 422


def test_routes_work_when_the_recorder_is_disabled(tmp_path, monkeypatch, clock):
    """Past sessions stay replayable with GCS_BLACKBOX=0."""
    d = tmp_path / "blackbox"
    rec = _record(d, clock, ticks=5)
    monkeypatch.setenv("GCS_BLACKBOX", "0")
    monkeypatch.setattr(gcs_main, "_replay_store", None)
    with TestClient(gcs_main.app) as client:
        assert gcs_main.blackbox_service is None
        rows = client.get("/api/blackbox/sessions").json()["sessions"]
        assert [r["id"] for r in rows] == [os.path.basename(rec.path)[: -len(br.SESSION_SUFFIX)]]


def test_wide_fleet_is_thinned_by_the_sample_budget(tmp_path, clock):
    rec = _record(tmp_path / "bb", clock, ticks=200, drones=20)
    s = br.ReplaySession.load(rec.path, "x")
    out = s.frames(max_samples=1000)  # 1000 / 20 drones = 50 frames
    assert out["count"] <= 50 and out["stride"] >= 4
    assert all(len(f["ids"]) == 20 for f in out["frames"])
    one = s.frames(max_samples=1000, drones=[3])  # one drone: the same budget buys 20x the frames
    assert one["count"] == 200 and one["stride"] == 1


# --------------------------------------------------- operator ids and addresses


def test_redaction_pseudonymises_operators_hides_addresses_and_keeps_system():
    ev = {"type": "event", "kind": "audit", "operator": "alice", "requested_by": "bob", "origin_ip": "10.1.2.3",
          "role": "FLIGHT_COMMANDER", "reason": "called from 192.168.0.7 by phone", "nested": [{"operator": "system"}],
          "origin_ip_missing": None, "t": 5}
    out = br.redact_identities(ev)
    assert out["operator"] == br.pseudonym("alice") != "alice" and out["operator"].startswith("operator-")
    assert br.pseudonym("alice") == br.pseudonym("alice") and br.pseudonym("alice") != br.pseudonym("bob")
    assert out["requested_by"] == br.pseudonym("bob")
    assert out["origin_ip"] == br.HIDDEN and "192.168.0.7" not in out["reason"] and br.HIDDEN in out["reason"]
    assert out["nested"] == [{"operator": "system"}]  # the GCS itself is not a person
    assert out["role"] == "FLIGHT_COMMANDER" and out["t"] == 5  # roles and times are not identities
    assert ev["operator"] == "alice"  # the input is not modified


def test_redaction_leaves_a_missing_ip_and_non_string_operator_alone():
    assert br.redact_identities({"origin_ip": None, "operator": None}) == {"origin_ip": None, "operator": None}


def test_event_list_hides_identities_unless_asked(tmp_path, clock):
    ev = [("command", 10, {"command": "land", "operator": "alice", "trigger": "api", "origin_ip": "10.9.8.7"})]
    s = br.ReplaySession.load(_record(tmp_path / "bb", clock, events=ev).path, "x")
    hidden = s.event_list()
    row = hidden["events"][0]
    assert hidden["identities_hidden"] and row["operator"] == br.pseudonym("alice") and row["origin_ip"] == br.HIDDEN
    shown = s.event_list(reveal_identities=True)
    assert not shown["identities_hidden"] and shown["events"][0]["operator"] == "alice" and shown["events"][0]["origin_ip"] == "10.9.8.7"
    assert s.summary()["identities_hidden"] is True


def _as(role):
    from fastapi import Request

    from tara_gcs.auth import Operator

    op = Operator(f"test-{role.name.lower()}", role)

    def dep(req: Request):
        req.state.operator = op
        return op

    return dep


def test_viewer_and_choreographer_cannot_reveal_identities_and_see_pseudonyms_by_default(tmp_path, clock, monkeypatch):
    from tara_gcs.auth import Role

    d = tmp_path / "blackbox"
    rec = _record(d, clock, ticks=20, events=[("command", 5, {"command": "land", "operator": "alice", "trigger": "api", "origin_ip": "10.9.8.7"})])
    sid = os.path.basename(rec.path)[: -len(br.SESSION_SUFFIX)]
    monkeypatch.setattr(gcs_main, "blackbox_service", None)
    monkeypatch.setattr(gcs_main, "_replay_store", None)
    url = f"/api/blackbox/sessions/{sid}/events"
    with TestClient(gcs_main.app) as client:
        for role in (Role.VIEWER, Role.CHOREOGRAPHER):
            gcs_main.app.dependency_overrides[gcs_main.current_operator] = _as(role)
            body = client.get(url).json()
            assert body["identities_hidden"] and body["events"][0]["operator"] == br.pseudonym("alice")
            assert body["events"][0]["origin_ip"] == "[hidden]"
            assert "alice" not in client.get(url).text and "10.9.8.7" not in client.get(f"/api/blackbox/sessions/{sid}").text
            denied = client.get(url, params={"identities": True})
            assert denied.status_code == 403 and "alice" not in denied.text
        gcs_main.app.dependency_overrides[gcs_main.current_operator] = _as(Role.FLIGHT_COMMANDER)
        assert client.get(url).json()["events"][0]["operator"] != "alice"  # hidden by default even for a commander
        full = client.get(url, params={"identities": True}).json()
        assert not full["identities_hidden"] and full["events"][0]["operator"] == "alice" and full["events"][0]["origin_ip"] == "10.9.8.7"
