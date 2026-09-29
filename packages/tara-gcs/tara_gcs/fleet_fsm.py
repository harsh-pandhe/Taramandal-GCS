"""Fleet lifecycle state machine (issue #52).

Fleet state used to be inferred from loose flags (``is_running_trajectory``,
per-drone ``armed``/``connected``), so nothing stopped an operator from starting
a trajectory while the fleet was still climbing, or pressing Launch while it
was already flying. This module is the one place that decides what the fleet
is doing and which commands are legal right now:

    STAGED -> PREARM -> ARMED -> TAKEOFF -> FORMATION_HOLD -> RUNNING
           -> SHOW_COMPLETE -> LANDING -> DISARMED

plus two emergency states the issue's command list implies: ``RTL`` (returning
to launch) and ``ESTOP`` (motors killed). Transitions come from two places:

* **Operator commands** (``/api/launch``, ``/api/start-trajectory``, ...) are
  validated against :data:`COMMAND_RULES` before anything is sent to a
  vehicle; an illegal one raises :class:`IllegalTransitionError`, which the API
  maps to HTTP 409.
* **Observed progress** (first drone armed, climbing, formation altitude
  reached, show finished, fleet disarmed) is advanced by
  ``DroneManager._lifecycle_monitor_loop`` from telemetry, through the same
  :data:`TRANSITIONS` table, so an observed jump the table doesn't allow is
  just as impossible as a commanded one.

Emergency commands (LAND, RTL, ESTOP) are legal from *every* state, not just
the airborne ones. The machine starts in STAGED on every GCS start, so after a
restart mid-show it can believe the fleet is parked while drones are in the
air — an emergency command must never be refused on the strength of
bookkeeping that may be stale.

Every accepted transition and every rejected command is appended to a JSONL
audit log with timestamp, operator ID and trigger. A failed audit write is
logged and swallowed: a full disk must never be the reason an RTL is refused.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger("TaramandalGCS")

DEFAULT_AUDIT_PATH = os.path.join(os.path.dirname(__file__), "fleet_audit.jsonl")

#: Operator recorded for transitions the GCS makes on its own (telemetry-driven
#: progress, failsafes), as opposed to ones an operator commanded.
SYSTEM_OPERATOR = "system"


class LifecycleState(str, Enum):
    STAGED = "STAGED"
    PREARM = "PREARM"
    ARMED = "ARMED"
    TAKEOFF = "TAKEOFF"
    FORMATION_HOLD = "FORMATION_HOLD"
    RUNNING = "RUNNING"
    SHOW_COMPLETE = "SHOW_COMPLETE"
    LANDING = "LANDING"
    RTL = "RTL"
    ESTOP = "ESTOP"
    DISARMED = "DISARMED"


S = LifecycleState

#: One line per state, used in 409 messages and on the HUD so an operator sees
#: *why* a command was refused, not just the state's name.
STATE_DESCRIPTIONS: dict[LifecycleState, str] = {
    S.STAGED: "fleet on its pads, not yet launched",
    S.PREARM: "launch requested, waiting for drones to become armable",
    S.ARMED: "drones armed, climb not started",
    S.TAKEOFF: "fleet climbing to formation altitude, takeoff not complete",
    S.FORMATION_HOLD: "fleet hovering in formation, ready to start the show",
    S.RUNNING: "show trajectory playing",
    S.SHOW_COMPLETE: "show finished, fleet hovering at its final positions",
    S.LANDING: "fleet landing",
    S.RTL: "fleet returning to launch",
    S.ESTOP: "emergency stop, motors killed",
    S.DISARMED: "fleet on the ground, disarmed",
}

#: States any emergency command may enter from anywhere (see module docstring).
EMERGENCY_STATES = frozenset({S.LANDING, S.RTL, S.ESTOP})

#: States in which at least one drone may be armed. The lifecycle monitor
#: moves to DISARMED from any of these once no drone reports armed.
AIRBORNE_STATES = frozenset({
    S.ARMED, S.TAKEOFF, S.FORMATION_HOLD, S.RUNNING, S.SHOW_COMPLETE,
    S.LANDING, S.RTL, S.ESTOP,
})

#: Non-emergency transitions. Emergency targets are added on top of these by
#: :func:`can_transition`.
TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    S.STAGED: frozenset({S.PREARM}),
    # Back to STAGED when the launch sequence finishes without arming anyone.
    S.PREARM: frozenset({S.ARMED, S.STAGED}),
    S.ARMED: frozenset({S.TAKEOFF, S.DISARMED}),
    S.TAKEOFF: frozenset({S.FORMATION_HOLD, S.DISARMED}),
    S.FORMATION_HOLD: frozenset({S.RUNNING, S.DISARMED}),
    # FORMATION_HOLD: operator stopped playback, or playback failed; the fleet
    # hovers at its last setpoint either way.
    S.RUNNING: frozenset({S.SHOW_COMPLETE, S.FORMATION_HOLD, S.DISARMED}),
    S.SHOW_COMPLETE: frozenset({S.DISARMED}),
    S.LANDING: frozenset({S.DISARMED}),
    S.RTL: frozenset({S.DISARMED}),
    S.ESTOP: frozenset({S.DISARMED}),
    S.DISARMED: frozenset({S.PREARM}),
}


def can_transition(src: LifecycleState, dst: LifecycleState) -> bool:
    return dst in EMERGENCY_STATES or dst in TRANSITIONS[src]


class Command(str, Enum):
    LAUNCH = "launch"
    START_TRAJECTORY = "start_trajectory"
    STOP_TRAJECTORY = "stop_trajectory"
    LAND = "land"
    RTL = "rtl"
    ESTOP = "estop"
    UPLOAD_TRAJECTORY = "upload_trajectory"
    RESUME_SHOW = "resume_show"
    DISCARD_SHOW = "discard_show"


@dataclass(frozen=True)
class CommandRule:
    """Which states a command is legal in, and the state it moves the fleet to.

    Attributes:
        allowed_from: States the command may be issued in.
        target: State entered on success, or ``None`` for commands that are
            gated but don't change the lifecycle (e.g. uploading a trajectory).
        action: Human phrasing for error messages ("start the show").
    """
    allowed_from: frozenset[LifecycleState]
    target: LifecycleState | None
    action: str


_ALL_STATES = frozenset(LifecycleState)
# Swapping the show data out from under a playing trajectory is refused; any
# other time it's fine, including mid-air (e.g. loading the show while hovering).
_NOT_RUNNING = _ALL_STATES - {S.RUNNING}

COMMAND_RULES: dict[Command, CommandRule] = {
    Command.LAUNCH: CommandRule(frozenset({S.STAGED, S.DISARMED}), S.PREARM, "launch the fleet"),
    Command.START_TRAJECTORY: CommandRule(frozenset({S.FORMATION_HOLD}), S.RUNNING, "start the show trajectory"),
    Command.STOP_TRAJECTORY: CommandRule(frozenset({S.RUNNING}), S.FORMATION_HOLD, "stop the show trajectory"),
    Command.LAND: CommandRule(_ALL_STATES, S.LANDING, "land the fleet"),
    Command.RTL: CommandRule(_ALL_STATES, S.RTL, "return the fleet to launch"),
    Command.ESTOP: CommandRule(_ALL_STATES, S.ESTOP, "emergency-stop the fleet"),
    Command.UPLOAD_TRAJECTORY: CommandRule(_NOT_RUNNING, None, "upload a trajectory"),
    Command.RESUME_SHOW: CommandRule(_NOT_RUNNING, None, "resume a saved show"),
    Command.DISCARD_SHOW: CommandRule(_NOT_RUNNING, None, "discard a saved show"),
}

#: Stable ordering for listing states in messages and the API.
_STATE_ORDER = list(LifecycleState)


def _sorted_states(states) -> list[str]:
    return [s.value for s in _STATE_ORDER if s in states]


class IllegalTransitionError(Exception):
    """A command that isn't legal in the fleet's current state.

    ``detail()`` is the JSON body the API returns with its 409.
    """

    def __init__(self, command: Command, state: LifecycleState):
        rule = COMMAND_RULES[command]
        self.command = command
        self.state = state
        self.allowed_from = _sorted_states(rule.allowed_from)
        self.message = (
            f"Cannot {rule.action} while the fleet is {state.value} "
            f"({STATE_DESCRIPTIONS[state]}). "
            f"Allowed from: {', '.join(self.allowed_from)}."
        )
        super().__init__(self.message)

    def detail(self) -> dict:
        return {
            "error": "illegal_transition",
            "command": self.command.value,
            "state": self.state.value,
            "allowed_from": self.allowed_from,
            "message": self.message,
        }


def _utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


class FleetStateMachine:
    """The fleet's lifecycle state, its legal transitions, and their audit trail.

    Not thread-safe; every caller runs on the GCS's single asyncio loop, and no
    method awaits, so a check followed by a transition in the same synchronous
    block can't be interleaved with another command.
    """

    def __init__(self, audit_path: str | None = DEFAULT_AUDIT_PATH, history_size: int = 100):
        """
        Args:
            audit_path: JSONL file every transition and rejected command is
                appended to. ``None`` keeps the audit trail in memory only.
            history_size: How many recent transitions ``history`` keeps for the
                API and HUD. The file on disk is the complete record.
        """
        self.audit_path = audit_path
        self._state = S.STAGED
        self._since = time.time()
        # Bumped on every accepted transition, so the telemetry socket can tell
        # "the state changed since my last frame" with one integer compare.
        self._seq = 0
        self.history: deque[dict] = deque(maxlen=history_size)

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def seq(self) -> int:
        return self._seq

    def allowed_commands(self) -> list[str]:
        return [cmd.value for cmd, rule in COMMAND_RULES.items() if self._state in rule.allowed_from]

    def check(self, command: Command, *, operator: str, trigger: str) -> None:
        """Raise :class:`IllegalTransitionError` if ``command`` isn't legal now.

        Rejections are audited too: "who tried to start the show before
        takeoff finished" is exactly what a post-incident review asks.
        """
        if self._state in COMMAND_RULES[command].allowed_from:
            return
        err = IllegalTransitionError(command, self._state)
        logger.warning(f"Fleet lifecycle: rejected {command.value} from {operator} ({trigger}): {err.message}")
        now = time.time()
        self._audit({
            "ts": now,
            "time": _utc_iso(now),
            "accepted": False,
            "command": command.value,
            "state": self._state.value,
            "operator": operator,
            "trigger": trigger,
            "reason": err.message,
        })
        raise err

    def apply(self, command: Command, *, operator: str, trigger: str) -> dict | None:
        """Validate ``command`` and move to its target state.

        Returns the transition record, or ``None`` for a gated command that
        doesn't change state.
        """
        self.check(command, operator=operator, trigger=trigger)
        target = COMMAND_RULES[command].target
        if target is None:
            return None
        return self.transition(target, operator=operator, trigger=trigger)

    def transition(self, target: LifecycleState, *, operator: str, trigger: str) -> dict:
        """Move to ``target``. Raises ``ValueError`` if :data:`TRANSITIONS` forbids it.

        A ``ValueError`` here is a GCS bug (the command rules and the table
        disagree), not an operator error, which is why it isn't an
        :class:`IllegalTransitionError`.
        """
        src = self._state
        if not can_transition(src, target):
            raise ValueError(f"Transition {src.value} -> {target.value} is not in the lifecycle table")
        now = time.time()
        self._state = target
        self._since = now
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": now,
            "time": _utc_iso(now),
            "accepted": True,
            "from": src.value,
            "to": target.value,
            "operator": operator,
            "trigger": trigger,
        }
        self.history.append(record)
        logger.info(f"Fleet lifecycle: {src.value} -> {target.value} (operator={operator}, trigger={trigger})")
        self._audit(record)
        return record

    def advance(self, expected: LifecycleState, target: LifecycleState, *, trigger: str,
                operator: str = SYSTEM_OPERATOR) -> bool:
        """Transition only if still in ``expected``; for observed progress.

        The monitor loop and the playback task both call this, and either may
        find an operator command got there first (e.g. an RTL while the show
        was completing). Returns whether the transition happened.
        """
        if self._state is not expected:
            return False
        self.transition(target, operator=operator, trigger=trigger)
        return True

    def snapshot(self) -> dict:
        """The state as the API and the HUD's slow-frame channel report it."""
        return {
            "state": self._state.value,
            "description": STATE_DESCRIPTIONS[self._state],
            "since": self._since,
            "seq": self._seq,
            "allowed_commands": self.allowed_commands(),
            "last_transition": self.history[-1] if self.history else None,
        }

    def _audit(self, entry: dict) -> None:
        if not self.audit_path:
            return
        try:
            directory = os.path.dirname(self.audit_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError as e:
            logger.error(f"Fleet lifecycle: failed to write audit entry to {self.audit_path}: {e}")
