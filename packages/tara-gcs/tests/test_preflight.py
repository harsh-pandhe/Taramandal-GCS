"""Unit tests for the pre-arm checklist engine (issue #48).

The engine is pure logic over the telemetry dict, so these tests hand it
fabricated telemetry with an injected clock instead of running a fleet: every
threshold edge is then exact and repeatable. Where a check depends on what
DroneManager actually writes into that dict (link timestamps, health flags,
launching only the cleared drones) the test runs the real DroneManager on the
sim backend, as the rest of this suite does.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tara_gcs.drone_manager import DroneManager, _pad_offset
from tara_gcs.preflight import (
    DEFAULT_PROFILE,
    POLICIES,
    AuditLog,
    ConfirmationError,
    GateState,
    OperationalProfile,
    PreArmChecklistEngine,
    ProfilePolicy,
    Severity,
    gnss_fix_level,
    load_safety_config,
    show_hash,
    trajectory_hash,
)

N = 3
NOW = 1000.0


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_show(n: int = N) -> dict:
    show = {}
    for i in range(n):
        x, y = _pad_offset(i)
        show[i] = [
            {"time": 0.0, "x": x, "y": y, "z": 0.0, "yaw": 0.0},
            {"time": 5.0, "x": x, "y": y, "z": -3.0 - i, "yaw": 0.0},
        ]
    return show


def healthy_drone(i: int, show: dict, **overrides) -> dict:
    """Telemetry for a drone that passes every PRODUCTION gate."""
    x, y = _pad_offset(i)
    tel = {
        "id": i,
        "connected": True,
        "armed": False,
        "comm_latency_ms": 20.0,
        "link_stable_since": NOW - 30.0,
        "gps_fix_type": "RTK_FIXED",
        "battery_percent": 95.0,
        "battery_voltage": 16.76,
        "battery_cell_voltages": [4.19, 4.19, 4.18, 4.19],
        "health_armable": True,
        "health_home_ok": True,
        "mission_hash": trajectory_hash(show[i]),
        "local_x": 0.0,
        "local_y": 0.0,
        "local_z": 0.0,
        "fleet_x": x,
        "fleet_y": y,
        "fleet_z": 0.0,
    }
    tel.update(overrides)
    return tel


def fleet(show: dict, n: int = N, overrides: dict | None = None) -> dict:
    """{drone_id: telemetry}; overrides maps drone id -> field overrides."""
    overrides = overrides or {}
    return {i: healthy_drone(i, show, **overrides.get(i, {})) for i in range(n)}


def engine(profile, clock=None, audit=None) -> PreArmChecklistEngine:
    return PreArmChecklistEngine(profile, clock=clock or Clock(), audit_log=audit)


def latched(eng: PreArmChecklistEngine) -> PreArmChecklistEngine:
    eng.confirm_latch(eng.request_latch("alice")["token"], "alice")
    return eng


def gate(report: dict, drone_id: int, rule: str) -> dict:
    return next(g for g in report["drones"][str(drone_id)]["gates"] if g["rule"] == rule)


def fleet_gate(report: dict, rule: str) -> dict:
    return next(g for g in report["fleet_gates"] if g["rule"] == rule)


class FakeManager:
    def __init__(self, telemetry, trajectory=None, n=N):
        self.telemetry = telemetry
        self.ports = list(range(n))
        self.active_trajectory = trajectory
        self.geofence_radius = 30.0


# --- PRODUCTION ---------------------------------------------------------------

class TestProductionProfile:
    def test_fully_green_fleet_with_confirmed_latch_is_permitted(self):
        show = make_show()
        eng = latched(engine("PRODUCTION"))
        report = eng.evaluate(fleet(show), N, show)
        assert report["blocking_reasons"] == []
        assert report["launch_permitted"] is True
        assert report["launch_drone_ids"] == [0, 1, 2]
        assert all(g["state"] == "pass" for d in report["drones"].values() for g in d["gates"])

    def test_one_drone_without_rtk_fixed_rejects_the_whole_launch(self):
        show = make_show()
        report = latched(engine("PRODUCTION")).evaluate(
            fleet(show, overrides={1: {"gps_fix_type": "RTK_FLOAT"}}), N, show
        )
        assert report["launch_permitted"] is False
        assert report["launch_drone_ids"] == []
        assert gate(report, 1, "gnss_fix")["state"] == "fail"
        assert any("Drone 1" in r and "RTK_FLOAT" in r for r in report["blocking_reasons"])

    @pytest.mark.parametrize("soc, permitted", [(84.9, False), (85.0, True)])
    def test_battery_soc_floor_is_85_percent(self, soc, permitted):
        show = make_show()
        report = latched(engine("PRODUCTION")).evaluate(
            fleet(show, overrides={2: {"battery_percent": soc}}), N, show
        )
        assert report["launch_permitted"] is permitted

    def test_launch_without_confirmed_latch_is_rejected(self):
        show = make_show()
        report = engine("PRODUCTION").evaluate(fleet(show), N, show)
        assert fleet_gate(report, "operator_latch")["state"] == "fail"
        assert report["launch_permitted"] is False

    @pytest.mark.parametrize(
        "overrides, rule",
        [
            ({"battery_cell_voltages": [4.19, 4.14, 4.19, 4.19]}, "battery_cell_voltage"),
            ({"battery_cell_voltages": [4.20, 4.17, 4.20, 4.20]}, "battery_cell_balance"),
            ({"battery_cell_voltages": None}, "battery_cell_balance"),
            ({"comm_latency_ms": 150.0}, "comm_latency"),
            ({"comm_latency_ms": -1.0}, "comm_latency"),
            ({"comm_latency_ms": None}, "comm_latency"),
            ({"link_stable_since": NOW - 9.9}, "heartbeat"),
            ({"link_stable_since": None}, "heartbeat"),
            ({"health_home_ok": False}, "ekf_health"),
            ({"health_armable": False}, "ekf_health"),
            ({"mission_hash": None}, "trajectory_hash"),
            ({"mission_hash": "deadbeef"}, "trajectory_hash"),
            ({"local_x": 31.0}, "pad_clearance"),
            ({"connected": False}, "connected"),
        ],
    )
    def test_each_production_gate_is_blocking(self, overrides, rule):
        show = make_show()
        report = latched(engine("PRODUCTION")).evaluate(fleet(show, overrides={0: overrides}), N, show)
        assert gate(report, 0, rule)["state"] == "fail", gate(report, 0, rule)
        assert report["launch_permitted"] is False

    def test_pack_voltage_is_split_across_configured_cells_when_cells_are_not_reported(self):
        show = make_show()
        eng = PreArmChecklistEngine("PRODUCTION", battery_cells=6, clock=Clock())
        report = eng.evaluate(fleet(show, overrides={0: {"battery_cell_voltages": None, "battery_voltage": 24.6}}), N, show)
        g = gate(report, 0, "battery_cell_voltage")
        assert g["state"] == "fail" and "4.100 V/cell" in g["detail"] and "6S" in g["detail"]

    def test_two_drones_too_close_fail_pad_clearance(self):
        show = make_show()
        x0, y0 = _pad_offset(0)
        report = latched(engine("PRODUCTION")).evaluate(
            fleet(show, overrides={1: {"fleet_x": x0 + 1.0, "fleet_y": y0}}), N, show
        )
        assert gate(report, 0, "pad_clearance")["state"] == "fail"
        assert gate(report, 1, "pad_clearance")["state"] == "fail"
        assert gate(report, 2, "pad_clearance")["state"] == "pass"

    def test_missing_drone_fails_fleet_complete(self):
        show = make_show()
        report = latched(engine("PRODUCTION")).evaluate(fleet(show, n=2), N, show)
        assert fleet_gate(report, "fleet_complete")["state"] == "fail"
        assert report["launch_permitted"] is False

    def test_no_show_loaded_fails_trajectory(self):
        show = make_show()
        report = latched(engine("PRODUCTION")).evaluate(fleet(show), N, None)
        assert gate(report, 0, "trajectory_hash")["detail"] == "No show loaded"
        assert report["show_hash"] is None
        assert report["launch_permitted"] is False

    def test_drone_without_a_slot_fails_trajectory(self):
        show = make_show()
        partial = {k: v for k, v in show.items() if k != 2}
        report = latched(engine("PRODUCTION")).evaluate(fleet(show), N, partial)
        assert gate(report, 2, "trajectory_hash")["detail"] == "No trajectory slot for this drone"

    def test_nothing_is_overridable(self):
        eng = engine("PRODUCTION")
        for rule in ("gnss_fix", "battery_soc", "ekf_health", "operator_latch"):
            with pytest.raises(ConfirmationError, match="cannot be overridden under PRODUCTION"):
                eng.request_override([rule], "need to fly")


# --- SIMULATION ---------------------------------------------------------------

def sim_drone_overrides() -> dict:
    """What a sim fleet actually reports: 3D fix, no per-cell data, no onboard mission."""
    return {
        "gps_fix_type": "FIX_3D",
        "battery_cell_voltages": None,
        "mission_hash": None,
        "link_stable_since": NOW - 2.0,
    }


class TestSimulationProfile:
    def test_standard_3d_fix_launches_without_latch_or_rtk(self):
        show = make_show()
        tel = fleet(show, overrides={i: sim_drone_overrides() for i in range(N)})
        report = engine("SIMULATION").evaluate(tel, N, show)
        assert report["launch_permitted"] is True
        assert report["launch_drone_ids"] == [0, 1, 2]
        assert fleet_gate(report, "operator_latch")["state"] == "ignored"
        # Advisory rules are still evaluated and shown, just not blocking.
        assert gate(report, 0, "heartbeat")["state"] == "warn"
        assert gate(report, 0, "battery_cell_balance")["state"] == "ignored"

    def test_streamed_execution_passes_trajectory_hash(self):
        show = make_show()
        tel = fleet(show, overrides={i: sim_drone_overrides() for i in range(N)})
        report = engine("SIMULATION").evaluate(tel, N, show)
        assert gate(report, 0, "trajectory_hash")["state"] == "pass"
        assert "streamed" in gate(report, 0, "trajectory_hash")["detail"]

    def test_red_drone_is_left_out_rather_than_refusing_the_launch(self):
        show = make_show()
        overrides = {i: sim_drone_overrides() for i in range(N)}
        overrides[1]["gps_fix_type"] = "FIX_2D"
        overrides[2]["battery_percent"] = 49.0
        report = engine("SIMULATION").evaluate(fleet(show, overrides=overrides), N, show)
        assert report["launch_permitted"] is True
        assert report["launch_drone_ids"] == [0]
        assert report["excluded_drone_ids"] == [1, 2]

    def test_refused_when_no_drone_is_ready(self):
        show = make_show()
        overrides = {i: {**sim_drone_overrides(), "gps_fix_type": "NO_FIX"} for i in range(N)}
        report = engine("SIMULATION").evaluate(fleet(show, overrides=overrides), N, show)
        assert report["launch_permitted"] is False
        assert "No drone passes every blocking pre-arm gate." in report["blocking_reasons"]

    def test_missing_drone_is_only_a_warning(self):
        show = make_show()
        tel = fleet(show, n=2, overrides={i: sim_drone_overrides() for i in range(2)})
        report = engine("SIMULATION").evaluate(tel, N, show)
        assert fleet_gate(report, "fleet_complete")["state"] == "warn"
        assert report["launch_permitted"] is True


# --- ENGINEERING --------------------------------------------------------------

def bench_drone() -> dict:
    """A drone on the bench: tethered power, no GNSS antenna, no show."""
    return {
        "gps_fix_type": "NO_GPS",
        "battery_percent": 0.0,
        "battery_voltage": 12.0,
        "battery_cell_voltages": None,
        "mission_hash": None,
        "link_stable_since": NOW - 1.0,
        "comm_latency_ms": 400.0,
    }


class TestEngineeringProfile:
    def test_single_drone_bench_launch_without_rtk_or_battery(self):
        tel = fleet(make_show(), n=1, overrides={0: bench_drone()})
        report = engine("ENGINEERING").evaluate(tel, N, None)
        assert report["launch_permitted"] is True
        assert report["launch_drone_ids"] == [0]
        for rule in ("gnss_fix", "battery_soc", "comm_latency", "trajectory_hash"):
            assert gate(report, 0, rule)["state"] == "ignored"

    def test_ekf_health_still_blocks(self):
        tel = fleet(make_show(), n=1, overrides={0: {**bench_drone(), "health_home_ok": False}})
        report = engine("ENGINEERING").evaluate(tel, N, None)
        assert gate(report, 0, "ekf_health")["state"] == "fail"
        assert report["launch_permitted"] is False

    def test_two_step_override_bypasses_ekf_and_is_consumed_by_one_launch(self, tmp_path):
        audit = AuditLog(str(tmp_path / "audit.jsonl"))
        eng = engine("ENGINEERING", audit=audit)
        tel = fleet(make_show(), n=1, overrides={0: {**bench_drone(), "health_home_ok": False}})
        manager = FakeManager(tel)

        # Step 1 alone changes nothing.
        pending = eng.request_override(["ekf_health"], "bench: no GNSS antenna", operator="bob")
        assert eng.authorize_launch(manager).permitted is False

        eng.confirm_override(pending["token"], operator="carol")
        report = eng.evaluate_manager(manager)
        assert gate(report, 0, "ekf_health")["state"] == "overridden"
        assert next(r for r in report["rules"] if r["id"] == "ekf_health")["overridden"] is True

        decision = eng.authorize_launch(manager, operator="carol")
        assert decision.permitted is True
        assert decision.overridden_rules == {"ekf_health"}
        # Consumed: the next launch needs a fresh override.
        assert eng.authorize_launch(manager).permitted is False

        events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        kinds = [e["event"] for e in events]
        assert kinds == [
            "override_requested", "launch_rejected", "override_confirmed",
            "launch_authorized", "launch_rejected",
        ]
        confirmed = events[2]
        assert confirmed["operator"] == "carol" and confirmed["requested_by"] == "bob"
        assert confirmed["rules"] == ["ekf_health"] and confirmed["reason"] == "bench: no GNSS antenna"
        assert events[3]["overridden_rules"] == ["ekf_health"]

    def test_only_ekf_health_is_overridable(self):
        with pytest.raises(ConfirmationError, match="connected cannot be overridden"):
            engine("ENGINEERING").request_override(["connected", "ekf_health"], "why not")


# --- FIELD_TEST ---------------------------------------------------------------

class TestFieldTestProfile:
    @pytest.mark.parametrize("fix, permitted", [("RTK_FLOAT", True), ("RTK_FIXED", True), ("FIX_3D", False)])
    def test_accepts_rtk_float_or_fixed(self, fix, permitted):
        show = make_show()
        report = engine("FIELD_TEST").evaluate(fleet(show, overrides={0: {"gps_fix_type": fix}}), N, show)
        assert report["launch_permitted"] is permitted

    @pytest.mark.parametrize("soc, permitted", [(79.9, False), (80.0, True)])
    def test_battery_floor_is_80_percent(self, soc, permitted):
        show = make_show()
        report = engine("FIELD_TEST").evaluate(fleet(show, overrides={0: {"battery_percent": soc}}), N, show)
        assert report["launch_permitted"] is permitted

    def test_one_red_drone_refuses_the_whole_fleet(self):
        show = make_show()
        report = engine("FIELD_TEST").evaluate(fleet(show, overrides={2: {"connected": False}}), N, show)
        assert report["launch_permitted"] is False
        assert report["launch_drone_ids"] == []

    def test_cell_level_readings_are_advisory(self):
        show = make_show()
        report = engine("FIELD_TEST").evaluate(
            fleet(show, overrides={0: {"battery_cell_voltages": [4.0, 4.1, 4.1, 4.1]}}), N, show
        )
        assert gate(report, 0, "battery_cell_voltage")["state"] == "warn"
        assert gate(report, 0, "battery_cell_balance")["state"] == "warn"
        assert report["launch_permitted"] is True

    def test_onboard_hash_is_checked_when_reported_but_not_required(self):
        show = make_show()
        report = engine("FIELD_TEST").evaluate(
            fleet(show, overrides={0: {"mission_hash": None}, 1: {"mission_hash": "0" * 64}}), N, show
        )
        assert gate(report, 0, "trajectory_hash")["state"] == "pass"
        assert gate(report, 1, "trajectory_hash")["state"] == "fail"


# --- Two-step confirmation & audit ---------------------------------------------

class TestConfirmations:
    def test_wrong_token_is_refused_and_audited(self):
        eng = engine("PRODUCTION")
        eng.request_latch("alice")
        with pytest.raises(ConfirmationError, match="No pending latch request"):
            eng.confirm_latch("not-the-token", "alice")
        assert eng.audit.recent[-1]["event"] == "latch_confirm_rejected"
        assert eng.confirmation_state()["latch"]["state"] == "pending"

    def test_confirm_without_request_is_refused(self):
        with pytest.raises(ConfirmationError):
            engine("PRODUCTION").confirm_latch("anything")

    def test_step_two_must_come_inside_the_window(self):
        clock = Clock()
        eng = engine("PRODUCTION", clock=clock)
        token = eng.request_latch("alice")["token"]
        clock.t += eng.confirm_window_s + 0.1
        with pytest.raises(ConfirmationError, match="expired"):
            eng.confirm_latch(token, "alice")
        assert "latch_request_expired" in [e["event"] for e in eng.audit.recent]

    def test_confirmed_latch_expires_after_hold(self):
        clock = Clock()
        eng = latched(engine("PRODUCTION", clock=clock))
        assert eng.confirmation_state()["latch"]["state"] == "confirmed"
        clock.t += eng.hold_s + 0.1
        assert eng.confirmation_state()["latch"] == {"state": "idle"}
        assert eng.audit.recent[-1]["event"] == "latch_expired"

    def test_new_request_supersedes_the_old_token(self):
        eng = engine("PRODUCTION")
        first = eng.request_latch()["token"]
        second = eng.request_latch()["token"]
        with pytest.raises(ConfirmationError):
            eng.confirm_latch(first)
        eng.confirm_latch(second)

    def test_latch_authorises_exactly_one_launch(self):
        show = make_show()
        eng = latched(engine("PRODUCTION"))
        manager = FakeManager(fleet(show), show)
        first = eng.authorize_launch(manager, operator="alice")
        assert first.permitted is True and first.drone_ids == [0, 1, 2]
        second = eng.authorize_launch(manager)
        assert second.permitted is False
        assert fleet_gate(second.report, "operator_latch")["state"] == "fail"

    def test_override_needs_rules_and_a_reason(self):
        eng = engine("ENGINEERING")
        with pytest.raises(ConfirmationError, match="at least one rule"):
            eng.request_override([], "reason")
        with pytest.raises(ConfirmationError, match="reason"):
            eng.request_override(["ekf_health"], "   ")

    def test_refused_override_is_audited(self):
        eng = engine("PRODUCTION")
        with pytest.raises(ConfirmationError):
            eng.request_override(["gnss_fix"], "no base station today", operator="dave")
        entry = eng.audit.recent[-1]
        assert entry["event"] == "override_refused" and entry["operator"] == "dave"

    def test_pending_state_is_reported(self):
        eng = engine("ENGINEERING")
        eng.request_override(["ekf_health"], "bench", operator="bob")
        state = eng.confirmation_state()["override"]
        assert state["state"] == "pending" and state["rules"] == ["ekf_health"]
        assert state["expires_in_s"] == eng.confirm_window_s

    def test_audit_log_without_a_path_stays_in_memory(self):
        log = AuditLog(path=None)
        log.record("x", a=1)
        assert log.recent[-1]["event"] == "x" and log.recent[-1]["a"] == 1


# --- Policies, config, helpers ------------------------------------------------

class TestPolicies:
    def test_every_profile_has_a_policy(self):
        assert set(POLICIES) == set(OperationalProfile)

    def test_production_blocks_on_every_rule(self):
        policy = POLICIES[OperationalProfile.PRODUCTION]
        assert policy.overridable == frozenset()
        assert policy.allow_partial_fleet is False
        assert {r["severity"] for r in engine("PRODUCTION").evaluate({}, 0)["rules"]} == {"BLOCKING"}

    def test_unknown_rule_is_rejected(self):
        with pytest.raises(ValueError, match="unknown rule"):
            ProfilePolicy(OperationalProfile.SIMULATION, {"no_such_rule": Severity.BLOCKING})

    def test_overridable_rule_must_be_blocking(self):
        with pytest.raises(ValueError, match="not BLOCKING"):
            ProfilePolicy(
                OperationalProfile.SIMULATION, {"gnss_fix": Severity.WARNING},
                overridable=frozenset({"gnss_fix"}),
            )

    def test_unlisted_rule_is_ignored(self):
        policy = ProfilePolicy(OperationalProfile.SIMULATION, {})
        assert policy.severity("gnss_fix") is Severity.IGNORED

    def test_empty_fleet_is_never_permitted(self):
        report = engine("SIMULATION").evaluate({}, 0)
        assert report["launch_permitted"] is False
        assert fleet_gate(report, "fleet_complete")["state"] == "warn"


class TestSafetyConfig:
    def test_reads_profile_and_cells(self, tmp_path):
        path = tmp_path / "tara.toml"
        path.write_text('[safety]\noperational_profile = "field_test"\nbattery_cells = 6\n')
        config = load_safety_config(path, environ={})
        assert config.profile is OperationalProfile.FIELD_TEST
        assert config.battery_cells == 6

    def test_environment_overrides_file(self, tmp_path):
        path = tmp_path / "tara.toml"
        path.write_text('[safety]\noperational_profile = "PRODUCTION"\n')
        config = load_safety_config(path, environ={"TARA_OPERATIONAL_PROFILE": "ENGINEERING"})
        assert config.profile is OperationalProfile.ENGINEERING

    def test_missing_config_fails_closed_to_production(self, tmp_path):
        assert DEFAULT_PROFILE is OperationalProfile.PRODUCTION
        assert load_safety_config(tmp_path / "absent.toml", environ={}).profile is OperationalProfile.PRODUCTION
        assert load_safety_config(None, environ={}).profile is OperationalProfile.PRODUCTION

    def test_unknown_profile_refuses_to_start(self, tmp_path):
        path = tmp_path / "tara.toml"
        path.write_text('[safety]\noperational_profile = "YOLO"\n')
        with pytest.raises(ValueError, match="unknown operational_profile 'YOLO'"):
            load_safety_config(path, environ={})

    @pytest.mark.parametrize("cells", ["4", 0, -2])
    def test_bad_cell_count_is_rejected(self, tmp_path, cells):
        path = tmp_path / "tara.toml"
        path.write_text(f"[safety]\nbattery_cells = {json.dumps(cells)}\n")
        with pytest.raises(ValueError, match="battery_cells"):
            load_safety_config(path, environ={})

    def test_repo_tara_toml_selects_simulation(self):
        from tara_gcs import main as gcs_main

        config = load_safety_config(gcs_main.REPO_ROOT / "tara.toml", environ={})
        assert config.profile is OperationalProfile.SIMULATION


class TestHelpers:
    @pytest.mark.parametrize(
        "value, level",
        [("RTK_FIXED", 6), ("FixType.RTK_FLOAT", 5), ("fix_3d", 3), (6, 6), (None, 0), ("garbage", 0)],
    )
    def test_gnss_fix_level(self, value, level):
        assert gnss_fix_level(value) == level

    def test_hashes_are_independent_of_key_order_and_slot_key_type(self):
        a = [{"time": 0.0, "x": 1.0, "y": 2.0}]
        b = [{"y": 2.0, "x": 1.0, "time": 0.0}]
        assert trajectory_hash(a) == trajectory_hash(b)
        assert show_hash({0: a}) == show_hash({"0": b})
        assert trajectory_hash(a) != trajectory_hash([{"time": 0.0, "x": 1.0, "y": 2.1}])

    def test_string_slot_keys_are_matched(self):
        show = make_show()
        str_show = {str(k): v for k, v in show.items()}
        report = latched(engine("PRODUCTION")).evaluate(fleet(show), N, str_show)
        assert report["launch_permitted"] is True

    def test_gate_states_are_the_documented_set(self):
        assert {s.value for s in GateState} == {"pass", "fail", "warn", "overridden", "ignored"}


# --- DroneManager integration (real sim backend) -------------------------------

@pytest.mark.asyncio
async def test_drone_manager_feeds_the_checklist(tmp_path):
    """The fields the engine reads are actually populated by the monitor loops."""
    manager = DroneManager(ports=list(range(2)), backend="sim", state_path=str(tmp_path / "s.json"))
    try:
        await manager.connect_all()
        await asyncio.sleep(1.2)
        for tel in manager.telemetry.values():
            assert tel["link_stable_since"] is not None
            assert tel["health_armable"] is True and tel["health_home_ok"] is True
            assert tel["mission_hash"] is None

        report = PreArmChecklistEngine("SIMULATION").evaluate_manager(manager)
        assert report["launch_permitted"] is True, report["blocking_reasons"]
        assert report["launch_drone_ids"] == [0, 1]

        # Losing the link clears the stability timestamp.
        manager._sim_fleet.state.connected[1] = False
        await asyncio.sleep(0.3)
        assert manager.telemetry[1]["link_stable_since"] is None
        report = PreArmChecklistEngine("SIMULATION").evaluate_manager(manager)
        assert report["launch_drone_ids"] == [0]
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_launch_sequence_only_launches_cleared_drones_and_can_bypass_ekf(tmp_path):
    manager = DroneManager(ports=list(range(3)), backend="sim", state_path=str(tmp_path / "s.json"))
    try:
        await manager.connect_all()
        await asyncio.sleep(0.5)
        # Never armable: without the bypass, _launch_one would wait out
        # HOME_LOCK_TIMEOUT_S; with it, the drone arms straight away.
        manager._sim_fleet.state.is_armable = lambda drone_id: False

        await asyncio.wait_for(manager.launch_sequence(drone_ids=[1], require_ekf_health=False), timeout=5.0)
        await asyncio.sleep(0.3)
        assert [tel["armed"] for _, tel in sorted(manager.telemetry.items())] == [False, True, False]
    finally:
        await manager.shutdown()
