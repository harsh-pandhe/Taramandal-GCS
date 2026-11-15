"""Append-only audit log of AI proposals and the operator's decisions (issues #94, #89).

One file per project, inside the project's own directory so it moves to the
vault's ``.trash`` with the project::

    <vault>/<project id>/audit.jsonl     one JSON object per line, newest last

It is deliberately NOT part of ``project.json`` or ``show.json``: it is never in
the saved document, a save-history version, the exported show, the signed
manifest or what ``/api/validate`` reads. ``project_store.py`` does not know the
file exists, so a project save, a version restore or a rename cannot touch it.

Rules this module enforces:

  * Append only. There is no function here, and no route in ``audit_api.py``,
    that rewrites or deletes an entry.
  * The server owns identity: ``seq`` (1-based, gap-free), ``at``, ``projectId``,
    ``prev`` and ``hash``. A client cannot supply them.
  * Hash chain: ``hash`` is sha256 over the canonical JSON (sorted keys, compact
    separators, ASCII) of the entry without its own ``hash``; ``prev`` is the
    previous entry's ``hash`` ("" for the first). Reading recomputes the whole
    chain. This is tamper EVIDENCE, not prevention and not a signature: whoever
    can write the file can rewrite every hash, and removing whole lines from the
    END of the file leaves a shorter but valid chain.
  * Bounded, and never silently lossy: at ``MAX_ENTRIES`` entries or
    ``MAX_BYTES`` bytes the log writes one ``log_full`` marker and then refuses
    further appends (``AuditFull``). Nothing is dropped or rotated.

A torn tail (a crash mid-write left a last line without its newline) is kept as
evidence, never deleted. It is reported (``torn``), the chain is reported broken,
the next append ends the fragment with a newline and chains from the last
COMPLETE entry. The fragment then stays in the file as one unreadable line
(``corruptLines``), so ``chainOk`` stays false for that project: damage is never
healed silently.
"""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

from tara_studio.project_store import _LOCK, ProjectNotFound, ProjectStore

AUDIT_FILE = "audit.jsonl"
AUDIT_SCHEMA_VERSION = 1

#: Entries per project file, marker included. Module constants so tests can shrink them.
MAX_ENTRIES = 5000
#: Bytes per project file, marker included.
MAX_BYTES = 1_000_000
#: The client's part of one entry, serialized, must stay under this.
MAX_ENTRY_BYTES = 4096

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# The values engine/exportGate.js produces for `kind`. Kept by hand; a test
# reads the JS file and fails if the two disagree.
CHECK_KINDS = ("none", "open", "failed", "unverified")

FULL_DECISION = "log_full"
FULL_SUMMARY = "The audit log reached its size limit. No further entries are recorded for this project."
FULL_REQUEST_ID = "log_full"

_REQUEST_ID_RE = r"^[A-Za-z0-9_-]{1,64}$"


class AuditFull(Exception):
    """The log is at its cap; the entry was not stored."""


def _clean(value: str) -> str:
    """Refuses control characters (except newline and tab) and lone surrogates."""
    for ch in value:
        cat = unicodedata.category(ch)
        if cat == "Cs" or (cat == "Cc" and ch not in "\n\t"):
            raise ValueError("control characters are not allowed")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuditOp(_Strict):
    type: Literal["patch", "add", "move", "delete", "duplicate", "other"]
    sceneIndex: StrictInt | None = Field(None, ge=1)
    fields: list[Annotated[str, StringConstraints(max_length=40)]] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _no_control(self) -> AuditOp:
        for f in self.fields:
            _clean(f)
        return self


class AuditCheck(_Strict):
    status: Literal["PASSED", "FAILED", "REFUSED", "UNVERIFIED"]
    kind: Literal["none", "open", "failed", "unverified"] | None = None
    summary: str = Field(..., max_length=200)

    @model_validator(mode="after")
    def _no_control(self) -> AuditCheck:
        _clean(self.summary)
        return self


class AuditEntryIn(_Strict):
    """What a client may send. Everything else is the server's."""

    v: Literal[1]
    requestId: str = Field(..., pattern=_REQUEST_ID_RE)
    source: Literal["typed", "planner"]
    request: str = Field(..., max_length=200)
    summary: str = Field(..., max_length=200)
    ops: list[AuditOp] = Field(default_factory=list, max_length=20)
    check: AuditCheck | None = None
    decision: Literal[
        "applied", "dismissed", "stale", "blocked", "superseded", "refused",
        "planner_unavailable", "planner_timeout", "planner_failed",
    ]
    reason: str | None = Field(None, max_length=300)

    @model_validator(mode="after")
    def _checked(self) -> AuditEntryIn:
        _clean(self.request)
        _clean(self.summary)
        if self.reason is not None:
            _clean(self.reason)
        if len(_canonical(self.model_dump())) >= MAX_ENTRY_BYTES:
            raise ValueError(f"an entry must stay under {MAX_ENTRY_BYTES} bytes serialized")
        return self


def _canonical(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _entry_hash(entry: dict) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in entry.items() if k != "hash"})).hexdigest()


def _hash_matches(entry: dict) -> bool:
    try:
        return isinstance(entry.get("hash"), str) and entry["hash"] == _entry_hash(entry)
    except (ValueError, TypeError):  # e.g. a NaN a hand edit put in the file
        return False


def _identity(directory: Path) -> tuple[int, int] | None:
    """Which directory this is, so a project that was moved away and re-created
    under the same id is told apart from the one that was checked."""
    try:
        st = os.stat(directory)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


@dataclass(frozen=True)
class AuditRead:
    entries: list[dict]        # every intact entry, oldest first
    chain_ok: bool
    torn: bool                 # the file ends in a line with no newline
    corrupt_lines: int         # complete lines that are not a valid entry
    size: int


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")  # NaN/Infinity: a hand edit, and unserialisable in a response


def _scan(blob: bytes, project_id: str) -> AuditRead:
    """Parses and verifies a whole file. Never raises on file content."""
    torn = bool(blob) and not blob.endswith(b"\n")
    lines = blob.split(b"\n")
    lines.pop()  # after the final newline there is nothing (or the torn fragment, kept out)
    entries: list[dict] = []
    corrupt = 0
    ok = not torn
    prev = ""
    for raw in lines:
        try:
            entry = json.loads(raw.decode("utf-8"), parse_constant=_refuse_constant)
        except (ValueError, RecursionError):
            entry = None
        if not isinstance(entry, dict):
            corrupt += 1
            ok = False
            continue
        good = (
            entry.get("seq") == len(entries) + 1
            and entry.get("prev") == prev
            and entry.get("projectId") == project_id
            and _hash_matches(entry)
        )
        if not good:
            ok = False
        entries.append(entry)
        prev = entry["hash"] if isinstance(entry.get("hash"), str) else ""
    return AuditRead(entries, ok, torn, corrupt, len(blob))


class AuditLog:
    """The audit logs under one vault. Holds no state of its own."""

    def __init__(self, store: ProjectStore):
        self._store = store

    def _path(self, project_id: str):
        # Validates the id and raises ProjectNotFound for a missing or trashed
        # project, so a file is never created for a project that is not there.
        return self._store.existing_dir(project_id) / AUDIT_FILE

    def _read(self, project_id: str) -> AuditRead:
        try:
            blob = self._path(project_id).read_bytes()
        except FileNotFoundError:
            blob = b""
        return _scan(blob, project_id)

    @staticmethod
    def _is_full(read: AuditRead) -> bool:
        marker = bool(read.entries) and read.entries[-1].get("decision") == FULL_DECISION
        return marker or len(read.entries) >= MAX_ENTRIES

    def read(self, project_id: str, limit: int = DEFAULT_LIMIT) -> dict:
        """The latest ``limit`` entries, newest first, with the log's state."""
        with _LOCK:
            read = self._read(project_id)
        return {
            "entries": list(reversed(read.entries))[:limit],
            "total": len(read.entries),
            "full": self._is_full(read),
            "chainOk": read.chain_ok,
            "torn": read.torn,
            "corruptLines": read.corrupt_lines,
        }

    def append(self, project_id: str, body: AuditEntryIn) -> dict:
        """Stores one entry and returns it as stored.

        Raises:
            ProjectNotFound / InvalidProject: from the project store.
            AuditFull: the log is at its cap (a ``log_full`` marker is written
                the first time); ``body`` is not stored.
        """
        with _LOCK:
            # The lock is held from here to the last write: a project cannot be
            # deleted or re-created by another request in between (project_store
            # takes the same lock). The identity check below is a second line for
            # anything that bypasses it, so a write never lands in a project other
            # than the one that was checked.
            path = self._path(project_id)
            who = _identity(path.parent)
            read = self._read(project_id)
            if self._is_full(read):
                raise AuditFull(project_id)

            entry = self._seal(project_id, read, body.model_dump())
            line = _canonical(entry).decode("ascii") + "\n"
            reserve = len(self._marker_line(project_id, read)) + 8
            if len(read.entries) >= MAX_ENTRIES - 1 or read.size + len(line) + reserve > MAX_BYTES:
                # Without the marker the next reader could not tell "full" from "stopped".
                marker_entry = self._seal(project_id, read, self._marker_body())
                self._write(path, who, project_id, read, _canonical(marker_entry).decode("ascii") + "\n")
                raise AuditFull(project_id)

            self._write(path, who, project_id, read, line)
            return entry

    # ------------------------------------------------------------ internals

    def _seal(self, project_id: str, read: AuditRead, body: dict) -> dict:
        last = read.entries[-1] if read.entries else None
        entry = {
            **body,
            "seq": len(read.entries) + 1,
            "at": self._store._stamp(),
            "projectId": project_id,
            "prev": last["hash"] if last else "",
        }
        entry["hash"] = _entry_hash(entry)
        return entry

    @staticmethod
    def _marker_body() -> dict:
        return {
            "v": AUDIT_SCHEMA_VERSION, "requestId": FULL_REQUEST_ID, "source": "typed",
            "request": "", "summary": FULL_SUMMARY, "ops": [], "check": None,
            "decision": FULL_DECISION, "reason": None,
        }

    def _marker_line(self, project_id: str, read: AuditRead) -> str:
        """A marker as it would be written after one more entry (for its size)."""
        entry = {
            **self._marker_body(), "seq": len(read.entries) + 2, "at": self._store._stamp(),
            "projectId": project_id, "prev": "0" * 64, "hash": "0" * 64,
        }
        return _canonical(entry).decode("ascii") + "\n"

    @staticmethod
    def _write(path: Path, who, project_id: str, read: AuditRead, text: str) -> None:
        if _identity(path.parent) != who:
            raise ProjectNotFound(project_id)
        # A torn tail is ended with a newline first so the new entry starts on a
        # fresh line; the fragment itself is left in place.
        if read.torn:
            text = "\n" + text
        try:
            # "a" creates the file but never the directory.
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
        except FileNotFoundError:
            raise ProjectNotFound(project_id) from None
