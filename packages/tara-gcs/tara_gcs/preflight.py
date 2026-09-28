"""Deterministic pre-arm safety checklist engine and launch interlocks (issue #48).

Before this module, ``/api/launch`` could be called at any time: the only gate
was ``_launch_one`` waiting for ``is_armable``/``is_home_position_ok``, so a
drone with a flat battery, no RTK fix or the wrong show loaded would still arm.
Hardcoding production-grade limits everywhere would fix that and also make
bench testing and simulated fleets impossible, so the rules are fixed and the
*policy* varies.

* A **rule** is one deterministic check of telemetry against a threshold
  (``gnss_fix``, ``battery_soc`` ...). Rules never decide whether a failure
  matters.
* A :class:`ProfilePolicy` gives every rule a :class:`Severity` (BLOCKING,
  WARNING, IGNORED) and the thresholds to check it against, for one
  :class:`OperationalProfile`.
* :class:`PreArmChecklistEngine` evaluates the rules for the configured
  profile and turns the result into one launch decision.

The same telemetry always produces the same report: the engine reads the
``DroneManager.telemetry`` dict the monitor loops already maintain and never
polls a vehicle, so ``/api/preflight-check`` is as cheap as ``/api/status``.

Operator interaction goes through two-step confirmations (request, then
confirm with the returned token inside a short window), used for both the
PRODUCTION launch latch and for overriding a BLOCKING rule where a profile
allows it. Every step, and every launch accepted or refused, is appended to a
JSONL audit trail.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import secrets
import time
import tomllib
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Mapping

from tara_core import DEFAULT_LIMITS

logger = logging.getLogger("TaramandalGCS.preflight")


class OperationalProfile(str, Enum):
    SIMULATION = "SIMULATION"
    ENGINEERING = "ENGINEERING"
    FIELD_TEST = "FIELD_TEST"
    PRODUCTION = "PRODUCTION"


class Severity(str, Enum):
    #: A failure refuses the launch (or, in a partial-fleet profile, keeps that
    #: drone out of it).
    BLOCKING = "BLOCKING"
    #: A failure is shown to the operator but never refuses anything.
    WARNING = "WARNING"
    #: Not evaluated at all under this profile.
    IGNORED = "IGNORED"


class GateState(str, Enum):
    PASS = "pass"
    #: Failed a BLOCKING rule. The only state that refuses a launch.
    FAIL = "fail"
    #: Failed a WARNING rule.
    WARN = "warn"
    #: Failed a BLOCKING rule the operator has overridden with a confirmed
    #: two-step override.
    OVERRIDDEN = "overridden"
    IGNORED = "ignored"


#: MAVLink GPS_FIX_TYPE ordering, keyed by the name DroneManager stores in
#: ``telemetry["gps_fix_type"]`` (``str(mavsdk.telemetry.FixType.X)`` is the
#: bare member name). Kept here rather than importing mavsdk so the engine
#: stays importable, and testable, without the real-hardware dependency.
GNSS_FIX_TYPES = {
    "NO_GPS": 0,
    "NO_FIX": 1,
    "FIX_2D": 2,
    "FIX_3D": 3,
    "FIX_DGPS": 4,
    "RTK_FLOAT": 5,
    "RTK_FIXED": 6,
}
_FIX_TYPE_NAMES = {v: k for k, v in GNSS_FIX_TYPES.items()}


def gnss_fix_level(fix_type) -> int:
    """Numeric fix level for a stored fix type; 0 for anything unrecognised."""
    if isinstance(fix_type, int):
        return fix_type
    name = str(fix_type or "").rsplit(".", 1)[-1].upper()
    return GNSS_FIX_TYPES.get(name, 0)


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    #: "fleet" rules are evaluated once; "drone" rules once per drone.
    scope: str


#: Every rule the engine knows, in the order the HUD shows them.
RULES: tuple[Rule, ...] = (
    Rule("operator_latch", "Operator confirmation latch", "fleet"),
    Rule("fleet_complete", "Full fleet connected", "fleet"),
    Rule("connected", "Vehicle link up", "drone"),
    Rule("comm_latency", "Comm latency", "drone"),
    Rule("heartbeat", "Heartbeat continuity", "drone"),
    Rule("gnss_fix", "GNSS / RTK fix", "drone"),
    Rule("battery_soc", "Battery state of charge", "drone"),
    Rule("battery_cell_voltage", "Cell voltage", "drone"),
    Rule("battery_cell_balance", "Cell balance", "drone"),
    Rule("ekf_health", "EKF health & home lock", "drone"),
    Rule("trajectory_hash", "Trajectory hash match", "drone"),
    Rule("pad_clearance", "Launch pad clearance", "drone"),
)
RULES_BY_ID = {rule.id: rule for rule in RULES}


@dataclass(frozen=True)
class ProfilePolicy:
    """Severities and thresholds for one operational profile.

    Attributes:
        profile: Which profile this is.
        severities: Rule id -> severity. A rule missing here is IGNORED.
        overridable: BLOCKING rules an operator may override with a two-step
            confirmation. Empty for PRODUCTION: a zero-tolerance gate has
            nothing to override.
        allow_partial_fleet: If True, a drone failing a BLOCKING rule is left
            out of the launch instead of refusing it, and the launch proceeds
            with the drones that passed (refused only if none did). If False,
            one red drone refuses the whole launch.
        min_fix_type: Lowest acceptable ``GNSS_FIX_TYPES`` level.
        min_battery_pct: State-of-charge floor, percent.
        min_cell_voltage_v: Resting per-cell voltage floor.
        max_cell_delta_mv: Largest allowed spread between the highest and
            lowest cell, millivolts.
        max_latency_ms: Round-trip command latency ceiling.
        heartbeat_window_s: How long the link must have been up with no
            dropped heartbeat or failed probe.
        require_onboard_mission_hash: If True, a drone must report the hash of
            the mission it holds onboard; streamed execution (nothing onboard)
            fails. If False, only drones that do report one are compared.
    """

    profile: OperationalProfile
    severities: Mapping[str, Severity]
    overridable: frozenset[str] = frozenset()
    allow_partial_fleet: bool = False
    min_fix_type: int = GNSS_FIX_TYPES["RTK_FIXED"]
    min_battery_pct: float = 85.0
    min_cell_voltage_v: float = 4.15
    max_cell_delta_mv: float = 30.0
    max_latency_ms: float = 150.0
    heartbeat_window_s: float = 10.0
    require_onboard_mission_hash: bool = True

    def __post_init__(self) -> None:
        unknown = (set(self.severities) | set(self.overridable)) - set(RULES_BY_ID)
        if unknown:
            raise ValueError(f"unknown rule id(s) in {self.profile.value} policy: {sorted(unknown)}")
        for rule_id in self.overridable:
            if self.severity(rule_id) is not Severity.BLOCKING:
                # Overriding a WARNING/IGNORED rule would be a no-op, which
                # would make an audited override look like it did something.
                raise ValueError(f"{rule_id} is overridable but not BLOCKING in {self.profile.value}")

    def severity(self, rule_id: str) -> Severity:
        return self.severities.get(rule_id, Severity.IGNORED)


B, W, I = Severity.BLOCKING, Severity.WARNING, Severity.IGNORED

POLICIES: dict[OperationalProfile, ProfilePolicy] = {
    # Simulated fleets have no RTK base station and no real cells: a 3D fix and
    # a nominal charge are what a working sim reports, and the rest is advisory
    # so the HUD still shows what a real fleet would be judged on.
    OperationalProfile.SIMULATION: ProfilePolicy(
        profile=OperationalProfile.SIMULATION,
        severities={
            "operator_latch": I, "fleet_complete": W,
            "connected": B, "comm_latency": W, "heartbeat": W,
            "gnss_fix": B, "battery_soc": B,
            "battery_cell_voltage": I, "battery_cell_balance": I,
            "ekf_health": B, "trajectory_hash": W, "pad_clearance": W,
        },
        allow_partial_fleet=True,
        min_fix_type=GNSS_FIX_TYPES["FIX_3D"],
        min_battery_pct=50.0,
        require_onboard_mission_hash=False,
    ),
    # Bench testing: tethered power (battery readings are meaningless), no GNSS
    # antenna or RTK hardware, often one drone on the bench. EKF health is the
    # one check left BLOCKING, and it is the one an operator may bypass.
    OperationalProfile.ENGINEERING: ProfilePolicy(
        profile=OperationalProfile.ENGINEERING,
        severities={
            "operator_latch": I, "fleet_complete": I,
            "connected": B, "comm_latency": I, "heartbeat": I,
            "gnss_fix": I, "battery_soc": I,
            "battery_cell_voltage": I, "battery_cell_balance": I,
            "ekf_health": B, "trajectory_hash": I, "pad_clearance": I,
        },
        overridable=frozenset({"ekf_health"}),
        allow_partial_fleet=True,
        min_fix_type=GNSS_FIX_TYPES["NO_GPS"],
        min_battery_pct=0.0,
        require_onboard_mission_hash=False,
    ),
    # Real hardware flying, but not in front of an audience: RTK Float is
    # accepted, 80% charge floor, cell-level readings are advisory.
    OperationalProfile.FIELD_TEST: ProfilePolicy(
        profile=OperationalProfile.FIELD_TEST,
        severities={
            "operator_latch": I, "fleet_complete": B,
            "connected": B, "comm_latency": B, "heartbeat": W,
            "gnss_fix": B, "battery_soc": B,
            "battery_cell_voltage": W, "battery_cell_balance": W,
            "ekf_health": B, "trajectory_hash": B, "pad_clearance": B,
        },
        min_fix_type=GNSS_FIX_TYPES["RTK_FLOAT"],
        min_battery_pct=80.0,
        require_onboard_mission_hash=False,
    ),
    # Commercial air display: every rule BLOCKING, across 100% of the fleet,
    # nothing overridable.
    OperationalProfile.PRODUCTION: ProfilePolicy(
        profile=OperationalProfile.PRODUCTION,
        severities={rule.id: B for rule in RULES},
    ),
}

del B, W, I


def trajectory_hash(waypoints) -> str:
    """SHA-256 of one drone's waypoint list in a canonical JSON encoding.

    Canonical (sorted keys, no whitespace) so the GCS and anything that stores
    a copy onboard hash the same bytes for the same show.
    """
    payload = json.dumps(waypoints, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def show_hash(trajectory: Mapping) -> str:
    """SHA-256 over every slot of a show, keys normalised to strings."""
    return trajectory_hash({str(k): v for k, v in trajectory.items()})


def _slot_waypoints(trajectory: Mapping, drone_id: int):
    if drone_id in trajectory:
        return trajectory[drone_id]
    return trajectory.get(str(drone_id))


# --- Safety configuration (tara.toml [safety]) ------------------------------

#: Used when tara.toml has no [safety] table (or no tara.toml exists at all).
#: Fails closed: a GCS that cannot tell which profile it is meant to enforce
#: enforces the strictest one, rather than silently relaxing to SIMULATION.
DEFAULT_PROFILE = OperationalProfile.PRODUCTION

#: Series cell count used to turn pack voltage into per-cell voltage.
DEFAULT_BATTERY_CELLS = 4

#: Environment override for the profile, for scripted runs that should not edit tara.toml.
PROFILE_ENV_VAR = "TARA_OPERATIONAL_PROFILE"


@dataclass(frozen=True)
class SafetyConfig:
    profile: OperationalProfile = DEFAULT_PROFILE
    battery_cells: int = DEFAULT_BATTERY_CELLS


def parse_profile(value) -> OperationalProfile:
    if isinstance(value, OperationalProfile):
        return value
    try:
        return OperationalProfile(str(value).strip().upper())
    except ValueError:
        valid = ", ".join(p.value for p in OperationalProfile)
        raise ValueError(f"unknown operational_profile {value!r} (expected one of: {valid})") from None


def load_safety_config(toml_path: Path | str | None, environ: Mapping[str, str] = os.environ) -> SafetyConfig:
    """Reads ``[safety]`` from tara.toml, with ``TARA_OPERATIONAL_PROFILE`` taking precedence.

    An unknown profile name raises rather than falling back: a typo in the
    safety profile should stop the GCS from starting, not quietly pick one.
    """
    section: dict = {}
    if toml_path is not None and Path(toml_path).exists():
        section = tomllib.loads(Path(toml_path).read_text()).get("safety", {})

    raw_profile = environ.get(PROFILE_ENV_VAR) or section.get("operational_profile")
    profile = parse_profile(raw_profile) if raw_profile else DEFAULT_PROFILE

    cells = section.get("battery_cells", DEFAULT_BATTERY_CELLS)
    if not isinstance(cells, int) or cells < 1:
        raise ValueError(f"[safety] battery_cells must be a positive integer, got {cells!r}")
    return SafetyConfig(profile=profile, battery_cells=cells)


# --- Audit trail -------------------------------------------------------------

DEFAULT_AUDIT_PATH = os.path.join(os.path.dirname(__file__), "audit", "preflight_audit.jsonl")


class AuditLog:
    """Append-only JSONL record of every operator confirmation and launch decision.

    Each entry is flushed and fsynced as it is written: the point of an audit
    trail is that it survives the crash it might be needed to explain. Recent
    entries are also kept in memory for the HUD.
    """

    def __init__(self, path: str | None = DEFAULT_AUDIT_PATH, keep_recent: int = 200):
        self.path = path
        self.recent: deque[dict] = deque(maxlen=keep_recent)

    def record(self, event: str, **fields) -> dict:
        entry = {"ts": time.time(), "event": event, **fields}
        self.recent.append(entry)
        logger.info("AUDIT %s", json.dumps(entry, sort_keys=True, default=str))
        if self.path:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return entry


# --- Two-step confirmations --------------------------------------------------

class ConfirmationError(ValueError):
    """A confirmation step was refused (unknown/expired token, disallowed rule...)."""


@dataclass
class _Pending:
    token: str
    operator: str
    expires_at: float
    rules: frozenset[str] = frozenset()
    reason: str = ""


@dataclass
class _Active:
    operator: str
    confirmed_at: float
    expires_at: float
    rules: frozenset[str] = frozenset()
    reason: str = ""


# --- Engine ------------------------------------------------------------------

@dataclass
class LaunchDecision:
    permitted: bool
    report: dict
    #: Drones to hand to DroneManager.launch_sequence (only meaningful if permitted).
    drone_ids: list[int] = field(default_factory=list)
    #: Rules whose BLOCKING failures were overridden for this launch.
    overridden_rules: frozenset[str] = frozenset()


class PreArmChecklistEngine:
    """Evaluates the pre-arm rules for one operational profile.

    Args:
        profile: The profile to enforce. Fixed for the engine's lifetime; it
            comes from tara.toml, never from the API, so a browser cannot talk
            a PRODUCTION GCS down into SIMULATION.
        battery_cells: Series cell count, for per-cell voltage.
        audit_log: Where confirmations and launch decisions are recorded.
        clock: Monotonic time source (injectable for tests). Must be the same
            clock DroneManager stamps ``link_stable_since`` with.
        confirm_window_s: Time allowed between step 1 and step 2 of a
            two-step confirmation.
        hold_s: How long a confirmed latch/override stays active if no launch
            consumes it.
    """

    CONFIRM_WINDOW_S = 30.0
    HOLD_S = 120.0

    def __init__(
        self,
        profile: OperationalProfile | str = DEFAULT_PROFILE,
        battery_cells: int = DEFAULT_BATTERY_CELLS,
        audit_log: AuditLog | None = None,
        clock: Callable[[], float] = time.monotonic,
        confirm_window_s: float = CONFIRM_WINDOW_S,
        hold_s: float = HOLD_S,
    ):
        self.profile = parse_profile(profile)
        self.policy = POLICIES[self.profile]
        self.battery_cells = battery_cells
        self.audit = audit_log if audit_log is not None else AuditLog(path=None)
        self._clock = clock
        self.confirm_window_s = confirm_window_s
        self.hold_s = hold_s
        self._pending: dict[str, _Pending] = {}  # kind ("latch"/"override") -> pending step 1
        self._active: dict[str, _Active] = {}  # kind -> confirmed step 2

    # --- Two-step confirmation -------------------------------------------

    def _expire(self) -> None:
        now = self._clock()
        for kind, pending in list(self._pending.items()):
            if now > pending.expires_at:
                del self._pending[kind]
                self.audit.record(f"{kind}_request_expired", profile=self.profile.value, operator=pending.operator)
        for kind, active in list(self._active.items()):
            if now > active.expires_at:
                del self._active[kind]
                self.audit.record(f"{kind}_expired", profile=self.profile.value, operator=active.operator)

    def _request(self, kind: str, operator: str, rules: frozenset[str] = frozenset(), reason: str = "") -> dict:
        self._expire()
        token = secrets.token_urlsafe(16)
        expires_at = self._clock() + self.confirm_window_s
        # A new request supersedes any earlier unconfirmed one of the same kind.
        self._pending[kind] = _Pending(token, operator, expires_at, rules, reason)
        self.audit.record(
            f"{kind}_requested", profile=self.profile.value, operator=operator,
            rules=sorted(rules), reason=reason,
        )
        return {"token": token, "expires_in_s": self.confirm_window_s, "rules": sorted(rules)}

    def _confirm(self, kind: str, token: str, operator: str, gates: dict | None = None) -> dict:
        self._expire()
        pending = self._pending.get(kind)
        if pending is None or not secrets.compare_digest(pending.token, token or ""):
            self.audit.record(f"{kind}_confirm_rejected", profile=self.profile.value, operator=operator)
            raise ConfirmationError(
                f"No pending {kind} request matches that token — it may have expired "
                f"({self.confirm_window_s:.0f}s window). Start again from step 1."
            )
        del self._pending[kind]
        now = self._clock()
        self._active[kind] = _Active(operator, now, now + self.hold_s, pending.rules, pending.reason)
        self.audit.record(
            f"{kind}_confirmed", profile=self.profile.value, operator=operator,
            requested_by=pending.operator, rules=sorted(pending.rules), reason=pending.reason,
            gates=gates,
        )
        return {"confirmed": True, "expires_in_s": self.hold_s, "rules": sorted(pending.rules)}

    def request_latch(self, operator: str = "operator") -> dict:
        """Step 1 of the launch confirmation latch."""
        return self._request("latch", operator)

    def confirm_latch(self, token: str, operator: str = "operator", gates: dict | None = None) -> dict:
        """Step 2 of the launch confirmation latch. ``gates`` is recorded in the audit entry."""
        return self._confirm("latch", token, operator, gates)

    def request_override(self, rules: Iterable[str], reason: str, operator: str = "operator") -> dict:
        """Step 1 of overriding BLOCKING rules. Only rules the profile marks overridable are accepted."""
        rules = frozenset(rules)
        if not rules:
            raise ConfirmationError("An override must name at least one rule.")
        if not (reason or "").strip():
            raise ConfirmationError("An override needs a reason for the audit trail.")
        refused = sorted(rules - self.policy.overridable)
        if refused:
            self.audit.record(
                "override_refused", profile=self.profile.value, operator=operator,
                rules=sorted(rules), reason=reason,
            )
            allowed = ", ".join(sorted(self.policy.overridable)) or "none"
            raise ConfirmationError(
                f"{', '.join(refused)} cannot be overridden under {self.profile.value} "
                f"(overridable here: {allowed})."
            )
        return self._request("override", operator, rules, reason.strip())

    def confirm_override(self, token: str, operator: str = "operator") -> dict:
        """Step 2 of an override."""
        return self._confirm("override", token, operator)

    def confirmation_state(self) -> dict:
        self._expire()
        now = self._clock()

        def describe(kind):
            if kind in self._active:
                a = self._active[kind]
                return {"state": "confirmed", "operator": a.operator, "rules": sorted(a.rules),
                        "reason": a.reason, "expires_in_s": round(a.expires_at - now, 1)}
            if kind in self._pending:
                p = self._pending[kind]
                return {"state": "pending", "operator": p.operator, "rules": sorted(p.rules),
                        "reason": p.reason, "expires_in_s": round(p.expires_at - now, 1)}
            return {"state": "idle"}

        return {"latch": describe("latch"), "override": describe("override")}

    @property
    def overridden_rules(self) -> frozenset[str]:
        self._expire()
        active = self._active.get("override")
        return active.rules if active else frozenset()

    # --- Evaluation --------------------------------------------------------

    def evaluate(
        self,
        telemetry: Mapping[int, Mapping],
        expected_drones: int,
        trajectory: Mapping | None = None,
        geofence_radius_m: float = DEFAULT_LIMITS.geofence_radius_m,
    ) -> dict:
        """Evaluates every rule and returns the full per-drone / per-rule report.

        Args:
            telemetry: ``DroneManager.telemetry``.
            expected_drones: Fleet size the GCS was configured for
                (``len(DroneManager.ports)``); drones that never connected are
                absent from ``telemetry`` and only show up as a shortfall here.
            trajectory: The active show, slot id -> waypoint list.
            geofence_radius_m: ``DroneManager.geofence_radius``.
        """
        self._expire()
        policy = self.policy
        overridden = self.overridden_rules
        now = self._clock()

        drones: dict[str, dict] = {}
        ready_ids: list[int] = []
        blocking: list[str] = []

        positions = {
            d_id: (tel.get("fleet_x", 0.0), tel.get("fleet_y", 0.0), tel.get("fleet_z", 0.0))
            for d_id, tel in telemetry.items() if tel.get("connected")
        }

        for drone_id in sorted(telemetry):
            tel = telemetry[drone_id]
            gates = [
                self._gate(rule_id, passed, detail, overridden)
                for rule_id, passed, detail in self._drone_checks(
                    drone_id, tel, trajectory, geofence_radius_m, positions, now
                )
            ]
            failed = [g for g in gates if g["state"] == GateState.FAIL.value]
            ready = not failed
            if ready:
                ready_ids.append(drone_id)
            elif not policy.allow_partial_fleet:
                blocking.extend(f"Drone {drone_id}: {g['title']} — {g['detail']}" for g in failed)
            drones[str(drone_id)] = {"ready": ready, "armed": bool(tel.get("armed")), "gates": gates}

        connected = sum(1 for t in telemetry.values() if t.get("connected"))
        latch = self._active.get("latch")
        fleet_gates = [
            self._gate(
                "operator_latch",
                latch is not None,
                (f"Confirmed by {latch.operator}, {latch.expires_at - now:.0f}s left"
                 if latch else "Not confirmed — arm and confirm the launch latch"),
                overridden,
            ),
            self._gate(
                "fleet_complete",
                expected_drones > 0 and connected >= expected_drones,
                f"{connected}/{expected_drones} drones connected",
                overridden,
            ),
        ]
        blocking = [f"Fleet: {g['title']} — {g['detail']}" for g in fleet_gates
                    if g["state"] == GateState.FAIL.value] + blocking

        if not ready_ids:
            blocking.append("No drone passes every blocking pre-arm gate.")

        permitted = not blocking
        return {
            "profile": self.profile.value,
            "evaluated_at": time.time(),
            "launch_permitted": permitted,
            "launch_drone_ids": ready_ids if permitted else [],
            "excluded_drone_ids": sorted(set(telemetry) - set(ready_ids)),
            "blocking_reasons": blocking,
            "allow_partial_fleet": policy.allow_partial_fleet,
            "expected_drones": expected_drones,
            "show_hash": show_hash(trajectory) if trajectory else None,
            "rules": [
                {
                    "id": rule.id,
                    "title": rule.title,
                    "scope": rule.scope,
                    "severity": policy.severity(rule.id).value,
                    "overridable": rule.id in policy.overridable,
                    "overridden": rule.id in overridden,
                }
                for rule in RULES
            ],
            "fleet_gates": fleet_gates,
            "drones": drones,
            "confirmations": self.confirmation_state(),
        }

    def evaluate_manager(self, manager) -> dict:
        """``evaluate`` against a live DroneManager."""
        return self.evaluate(
            manager.telemetry,
            expected_drones=len(manager.ports),
            trajectory=manager.active_trajectory,
            geofence_radius_m=manager.geofence_radius,
        )

    def authorize_launch(self, manager, operator: str = "operator") -> LaunchDecision:
        """Evaluates the gates for a launch request and records the decision.

        On success the confirmed latch and override are consumed: each
        confirmation authorises exactly one launch.
        """
        report = self.evaluate_manager(manager)
        if not report["launch_permitted"]:
            self.audit.record(
                "launch_rejected", profile=self.profile.value, operator=operator,
                reasons=report["blocking_reasons"],
            )
            return LaunchDecision(False, report)

        overridden = self.overridden_rules
        self.audit.record(
            "launch_authorized", profile=self.profile.value, operator=operator,
            drone_ids=report["launch_drone_ids"], excluded=report["excluded_drone_ids"],
            overridden_rules=sorted(overridden), show_hash=report["show_hash"],
        )
        self._active.pop("latch", None)
        self._active.pop("override", None)
        return LaunchDecision(True, report, report["launch_drone_ids"], overridden)

    # --- Rules -------------------------------------------------------------

    def _gate(self, rule_id: str, passed: bool, detail: str, overridden: frozenset[str]) -> dict:
        severity = self.policy.severity(rule_id)
        if severity is Severity.IGNORED:
            state = GateState.IGNORED
        elif passed:
            state = GateState.PASS
        elif severity is Severity.WARNING:
            state = GateState.WARN
        elif rule_id in overridden:
            state = GateState.OVERRIDDEN
        else:
            state = GateState.FAIL
        return {
            "rule": rule_id,
            "title": RULES_BY_ID[rule_id].title,
            "severity": severity.value,
            "passed": bool(passed),
            "state": state.value,
            "detail": detail,
        }

    def _drone_checks(self, drone_id, tel, trajectory, geofence_radius_m, positions, now):
        """Yields (rule_id, passed, detail) for every per-drone rule."""
        p = self.policy
        connected = bool(tel.get("connected"))
        yield "connected", connected, "Link up" if connected else "No link to vehicle"

        latency = tel.get("comm_latency_ms")
        if not connected or latency is None:
            yield "comm_latency", False, "No latency measurement"
        elif latency < 0:
            yield "comm_latency", False, "Latency probe failed"
        else:
            yield ("comm_latency", latency < p.max_latency_ms,
                   f"{latency:.0f} ms (limit < {p.max_latency_ms:.0f} ms)")

        stable_since = tel.get("link_stable_since")
        stable_s = (now - stable_since) if (connected and stable_since is not None) else 0.0
        yield ("heartbeat", stable_s >= p.heartbeat_window_s,
               f"{stable_s:.1f}s without a dropped heartbeat (need {p.heartbeat_window_s:.0f}s)")

        fix_name = str(tel.get("gps_fix_type") or "NO_GPS").rsplit(".", 1)[-1]
        fix = gnss_fix_level(fix_name)
        yield ("gnss_fix", connected and fix >= p.min_fix_type,
               f"{fix_name} (need {_FIX_TYPE_NAMES[p.min_fix_type]} or better)")

        soc = float(tel.get("battery_percent") or 0.0)
        yield ("battery_soc", connected and soc >= p.min_battery_pct,
               f"{soc:.1f}% (floor {p.min_battery_pct:.0f}%)")

        cells = tel.get("battery_cell_voltages")
        pack_v = float(tel.get("battery_voltage") or 0.0)
        if cells:
            cell_v = min(cells)
            cell_detail = f"min cell {cell_v:.3f} V"
        else:
            cell_v = pack_v / self.battery_cells
            cell_detail = f"{cell_v:.3f} V/cell ({pack_v:.2f} V / {self.battery_cells}S)"
        yield ("battery_cell_voltage", connected and cell_v >= p.min_cell_voltage_v,
               f"{cell_detail} (floor {p.min_cell_voltage_v:.2f} V)")

        if cells:
            delta_mv = (max(cells) - min(cells)) * 1000.0
            yield ("battery_cell_balance", connected and delta_mv < p.max_cell_delta_mv,
                   f"{delta_mv:.0f} mV spread (limit < {p.max_cell_delta_mv:.0f} mV)")
        else:
            # Zero tolerance means "cannot verify" is a failure, not a pass.
            yield "battery_cell_balance", False, "Per-cell voltages not reported by this vehicle"

        armable = bool(tel.get("health_armable"))
        home_ok = bool(tel.get("health_home_ok"))
        if armable and home_ok:
            ekf_detail = "Armable, home position locked"
        else:
            missing = [name for name, ok in (("not armable", armable), ("no home lock", home_ok)) if not ok]
            ekf_detail = ", ".join(missing).capitalize()
        yield "ekf_health", connected and armable and home_ok, ekf_detail

        yield ("trajectory_hash", *self._check_trajectory(drone_id, tel, trajectory))

        yield ("pad_clearance", *self._check_pad(drone_id, tel, geofence_radius_m, positions))

    def _check_trajectory(self, drone_id, tel, trajectory):
        if not trajectory:
            return False, "No show loaded"
        # Slot N is flown by drone N. That is exactly what
        # remap_trajectory_to_healthy_drones produces when the whole fleet is
        # healthy, which every profile that makes this rule BLOCKING requires.
        waypoints = _slot_waypoints(trajectory, drone_id)
        if not waypoints:
            return False, "No trajectory slot for this drone"
        expected = trajectory_hash(waypoints)
        onboard = tel.get("mission_hash")
        if onboard is None:
            if self.policy.require_onboard_mission_hash:
                return False, "Vehicle reports no onboard mission hash"
            return True, f"Slot hash {expected[:12]} (streamed from GCS)"
        if onboard != expected:
            return False, f"Onboard {str(onboard)[:12]} ≠ expected {expected[:12]}"
        return True, f"Onboard hash {expected[:12]} matches"

    def _check_pad(self, drone_id, tel, geofence_radius_m, positions):
        if not tel.get("connected"):
            return False, "No position (disconnected)"
        from_home = math.hypot(tel.get("local_x", 0.0), tel.get("local_y", 0.0))
        if from_home > geofence_radius_m:
            return False, f"{from_home:.1f} m from home, outside {geofence_radius_m:.0f} m geofence"
        own = positions.get(drone_id)
        nearest = min(
            (math.dist(own, pos) for other, pos in positions.items() if other != drone_id),
            default=math.inf,
        )
        limit = DEFAULT_LIMITS.min_separation_m
        if nearest < limit:
            return False, f"Another drone {nearest:.2f} m away (min {limit:.1f} m)"
        near = "no other drone" if math.isinf(nearest) else f"nearest drone {nearest:.1f} m"
        return True, f"Inside geofence, {near}"
