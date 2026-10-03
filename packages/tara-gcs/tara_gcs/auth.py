"""Operator authentication and role-based access control (issue #64).

Before this the GCS had no authentication: any process that could reach the
port could arm, launch or abort the fleet, and "who did it" in the audit log
was whatever the client chose to put in an ``X-Operator-ID`` header.

Model:

* **Operators** are named accounts with one :class:`Role` and a passphrase,
  kept in an operators file (default ``~/.config/taramandal/operators.json``).
  The file holds scrypt hashes, never passphrases, and must be readable by its
  owner only (``0600``): the GCS refuses to load one that group or others can
  read, the way ``ssh`` refuses a world-readable key. Accounts are managed
  with ``python -m tara_gcs.auth add|remove|list`` - there is deliberately no
  API to create one, so a compromised browser session cannot mint itself a
  commander account.
* **Sessions** are random bearer tokens issued by ``POST /api/auth/login`` and
  held in memory only. They expire (12 h by default) and all of them die when
  the GCS restarts. Only a SHA-256 of each token is stored, so even a memory
  dump of the GCS does not hand out usable tokens.
* **Roles** are ordered: a FLIGHT_COMMANDER can do everything a CHOREOGRAPHER
  can, who can do everything a VIEWER can.

Every check re-reads the operators file if it changed on disk, so removing an
operator or lowering their role takes effect on their very next request, not
when their session happens to expire.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import time
import tomllib
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Mapping


class Role(IntEnum):
    """What an operator may do. Higher roles include every lower one."""

    VIEWER = 1  # telemetry, status, audit trail
    CHOREOGRAPHER = 2  # upload and validate shows
    FLIGHT_COMMANDER = 3  # arm, launch, abort, mission upload, simulation


def parse_role(value: str) -> Role:
    try:
        return Role[value.strip().upper().replace("-", "_")]
    except KeyError:
        names = ", ".join(r.name.lower() for r in Role)
        raise ValueError(f"unknown role {value!r}; expected one of: {names}") from None


@dataclass(frozen=True)
class Operator:
    id: str
    role: Role

    def public(self) -> dict:
        return {"operator": self.id, "role": self.role.name}


class AuthError(Exception):
    """A request could not be authenticated (HTTP 401)."""


class LoginThrottled(AuthError):
    """Too many failed logins from one origin (HTTP 429)."""

    def __init__(self, message: str, retry_after_s: int):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class InsecureFileError(PermissionError):
    """The operators file is readable or writable by someone other than its owner."""


DEFAULT_OPERATORS_PATH = Path("~/.config/taramandal/operators.json").expanduser()
OPERATORS_PATH_ENV = "GCS_OPERATORS_PATH"
DEFAULT_SESSION_TTL_S = 12 * 3600

#: Operator ids appear in audit logs and HUDs; keep them plain.
_OPERATOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MIN_PASSPHRASE_LEN = 12

# scrypt cost: ~50 ms and 16 MiB per check on a laptop, which makes guessing
# expensive without making a legitimate login noticeably slow.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def _hash_passphrase(passphrase: str, salt: bytes, params: Mapping[str, int] = _SCRYPT) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt,
        n=params["n"], r=params["r"], p=params["p"], dklen=params["dklen"],
        maxmem=128 * 1024 * 1024,
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def check_owner_only(path: Path) -> None:
    """Raise InsecureFileError unless only the file's owner can read or write it."""
    if os.name != "posix":
        return  # POSIX mode bits don't describe Windows ACLs; nothing reliable to check.
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise InsecureFileError(
            f"{path} has permissions {mode:o}: group/others can access it. "
            f"Run `chmod 600 {path}`."
        )


class OperatorStore:
    """Operator accounts in a JSON file, re-read whenever it changes on disk."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._records: dict[str, dict] = {}
        self._stamp: tuple[float, int] | None = None
        # A fixed, valid record to hash against when the operator id is
        # unknown, so a failed login takes as long either way and does not
        # reveal which operator ids exist.
        self._decoy = {"salt": _b64(b"\0" * 16), "hash": _b64(b"\0" * 32), "scrypt": _SCRYPT}

    # -- file -------------------------------------------------------------

    def _refresh(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._records, self._stamp = {}, None
            return
        stamp = (st.st_mtime, st.st_size)
        if stamp == self._stamp:
            return
        check_owner_only(self.path)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        records = {}
        for rec in data.get("operators", []):
            parse_role(rec["role"])  # fail loudly on a hand-edited bad role
            records[rec["id"]] = rec
        self._records, self._stamp = records, stamp

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        # Created owner-only from the start (as `tara keygen` does), then
        # renamed over the old file, so it is never briefly readable by others
        # nor left half-written.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"operators": sorted(self._records.values(), key=lambda r: r["id"])}, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        self._stamp = None

    # -- accounts ---------------------------------------------------------

    def get(self, operator_id: str) -> Operator | None:
        self._refresh()
        rec = self._records.get(operator_id)
        return Operator(rec["id"], parse_role(rec["role"])) if rec else None

    def all(self) -> list[Operator]:
        self._refresh()
        return [Operator(r["id"], parse_role(r["role"])) for r in sorted(self._records.values(), key=lambda r: r["id"])]

    def add(self, operator_id: str, role: Role, passphrase: str, *, replace: bool = False) -> Operator:
        if not _OPERATOR_ID.match(operator_id):
            raise ValueError("operator id must be 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit")
        if len(passphrase) < MIN_PASSPHRASE_LEN:
            raise ValueError(f"passphrase must be at least {MIN_PASSPHRASE_LEN} characters")
        self._refresh()
        if operator_id in self._records and not replace:
            raise ValueError(f"operator {operator_id!r} already exists")
        salt = secrets.token_bytes(16)
        self._records[operator_id] = {
            "id": operator_id,
            "role": role.name,
            "salt": _b64(salt),
            "hash": _b64(_hash_passphrase(passphrase, salt)),
            "scrypt": dict(_SCRYPT),
        }
        self._write()
        return Operator(operator_id, role)

    def remove(self, operator_id: str) -> bool:
        self._refresh()
        if self._records.pop(operator_id, None) is None:
            return False
        self._write()
        return True

    def verify(self, operator_id: str, passphrase: str) -> Operator | None:
        """The operator if `passphrase` is theirs, else None. Constant-time per attempt."""
        self._refresh()
        rec = self._records.get(operator_id)
        target = rec or self._decoy
        computed = _hash_passphrase(passphrase, base64.b64decode(target["salt"]), target.get("scrypt", _SCRYPT))
        ok = hmac.compare_digest(computed, base64.b64decode(target["hash"]))
        return Operator(rec["id"], parse_role(rec["role"])) if (ok and rec) else None


@dataclass
class Session:
    operator_id: str
    issued_at: float
    expires_at: float


class SessionStore:
    """In-memory bearer sessions. Nothing here survives a restart, by design."""

    def __init__(self, ttl_s: float = DEFAULT_SESSION_TTL_S):
        self.ttl_s = ttl_s
        self._sessions: dict[str, Session] = {}

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(self, operator: Operator, now: float | None = None) -> tuple[str, Session]:
        now = time.time() if now is None else now
        self._sweep(now)
        token = secrets.token_urlsafe(32)
        session = Session(operator.id, now, now + self.ttl_s)
        self._sessions[self._key(token)] = session
        return token, session

    def resolve(self, token: str, now: float | None = None) -> Session | None:
        now = time.time() if now is None else now
        session = self._sessions.get(self._key(token))
        if session is None:
            return None
        if now >= session.expires_at:
            self._sessions.pop(self._key(token), None)
            return None
        return session

    def revoke(self, token: str) -> bool:
        return self._sessions.pop(self._key(token), None) is not None

    def revoke_operator(self, operator_id: str) -> int:
        doomed = [k for k, s in self._sessions.items() if s.operator_id == operator_id]
        for k in doomed:
            del self._sessions[k]
        return len(doomed)

    def _sweep(self, now: float) -> None:
        for k in [k for k, s in self._sessions.items() if now >= s.expires_at]:
            del self._sessions[k]


class LoginThrottle:
    """Refuses further logins from one origin after repeated failures."""

    def __init__(self, max_failures: int = 5, window_s: float = 60.0):
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, origin: str, now: float) -> deque[float]:
        q = self._failures[origin]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def retry_after(self, origin: str, now: float | None = None) -> float:
        """Seconds until `origin` may try again; 0 if it may try now."""
        now = time.time() if now is None else now
        q = self._recent(origin, now)
        if len(q) < self.max_failures:
            return 0.0
        return max(0.0, self.window_s - (now - q[0]))

    def failed(self, origin: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._recent(origin, now).append(now)

    def succeeded(self, origin: str) -> None:
        self._failures.pop(origin, None)


class AuthService:
    """Logins, sessions and per-request authentication, for the API layer."""

    def __init__(self, store: OperatorStore, sessions: SessionStore | None = None,
                 throttle: LoginThrottle | None = None):
        self.store = store
        self.sessions = sessions or SessionStore()
        self.throttle = throttle or LoginThrottle()

    def login(self, operator_id: str, passphrase: str, origin: str) -> tuple[str, Session, Operator]:
        wait = self.throttle.retry_after(origin)
        if wait:
            seconds = int(wait) + 1
            raise LoginThrottled(f"Too many failed logins from {origin}; try again in {seconds}s.", seconds)
        operator = self.store.verify(operator_id, passphrase)
        if operator is None:
            self.throttle.failed(origin)
            raise AuthError("Unknown operator or wrong passphrase.")
        self.throttle.succeeded(origin)
        token, session = self.sessions.issue(operator)
        return token, session, operator

    def authenticate(self, token: str | None) -> Operator:
        """The operator behind `token`, as the operators file says now."""
        if not token:
            raise AuthError("Not logged in.")
        session = self.sessions.resolve(token)
        if session is None:
            raise AuthError("Session expired or invalid; log in again.")
        operator = self.store.get(session.operator_id)
        if operator is None:
            # Removed from the operators file since logging in.
            self.sessions.revoke(token)
            raise AuthError("Operator no longer exists; log in again.")
        return operator


# --- configuration -----------------------------------------------------------

@dataclass(frozen=True)
class AuthConfig:
    operators_path: Path
    session_ttl_s: float


def load_auth_config(toml_path: Path | str | None, environ: Mapping[str, str] = os.environ) -> AuthConfig:
    """Reads ``[auth]`` from tara.toml; ``GCS_OPERATORS_PATH`` overrides the file location."""
    section: dict = {}
    if toml_path is not None and Path(toml_path).exists():
        section = tomllib.loads(Path(toml_path).read_text()).get("auth", {})
    raw_path = environ.get(OPERATORS_PATH_ENV) or section.get("operators_file")
    path = Path(raw_path).expanduser() if raw_path else DEFAULT_OPERATORS_PATH
    hours = section.get("session_ttl_hours", DEFAULT_SESSION_TTL_S / 3600)
    if not isinstance(hours, (int, float)) or not 0 < hours <= 24 * 7:
        raise ValueError(f"[auth] session_ttl_hours must be between 0 and 168, got {hours!r}")
    return AuthConfig(operators_path=path, session_ttl_s=float(hours) * 3600)


# --- operator management CLI ---------------------------------------------------

def _read_new_passphrase() -> str:
    first = getpass.getpass("Passphrase: ")
    if getpass.getpass("Repeat passphrase: ") != first:
        raise ValueError("passphrases do not match")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tara_gcs.auth",
        description="Manage GCS operator accounts (issue #64). Changes apply without restarting the GCS.",
    )
    parser.add_argument("--file", type=Path, help="Operators file (default: [auth].operators_file in tara.toml, "
                        f"${OPERATORS_PATH_ENV}, or {DEFAULT_OPERATORS_PATH}).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser("add", help="Create an operator, or reset one with --replace. Prompts for the passphrase.")
    add.add_argument("operator")
    add.add_argument("--role", required=True, type=parse_role,
                     help="viewer, choreographer or flight_commander")
    add.add_argument("--replace", action="store_true", help="Overwrite an existing operator's role and passphrase.")
    rm = sub.add_parser("remove", help="Delete an operator. Their sessions stop working immediately.")
    rm.add_argument("operator")
    sub.add_parser("list", help="Show operators and roles (never passphrases).")
    args = parser.parse_args(argv)

    path = args.file or load_auth_config(Path(__file__).resolve().parents[3] / "tara.toml").operators_path
    store = OperatorStore(path)
    try:
        if args.cmd == "add":
            op = store.add(args.operator, args.role, _read_new_passphrase(), replace=args.replace)
            print(f"[tara-gcs] {op.id} is a {op.role.name} in {path}")
        elif args.cmd == "remove":
            if not store.remove(args.operator):
                print(f"[tara-gcs] no operator {args.operator!r} in {path}", file=sys.stderr)
                return 1
            print(f"[tara-gcs] removed {args.operator} from {path}")
        else:
            ops = store.all()
            if not ops:
                print(f"[tara-gcs] no operators in {path}")
            for op in ops:
                print(f"{op.id}\t{op.role.name}")
    except (ValueError, InsecureFileError) as exc:
        print(f"[tara-gcs] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
