"""Server-side Project Vault for the Studio (issue #101).

Projects used to live only in one browser's localStorage. They are now files
under a vault directory on the Studio backend, so a show saved from one
browser opens in another and survives a cleared cache.

Deliberately plain files rather than a database, like the GCS's show state: a
project is something a designer can copy, back up or hand over. One directory
per project::

    <vault>/<project id>/project.json   the project: name, revision, document,
                                        save history, validation report
    <vault>/<project id>/show.json      the built trajectory, a show_v1 file
    <vault>/.trash/<project id>.<when>  deleted projects, kept for recovery

Two formats, each with one owner, instead of a third invented here:

  * ``show.json`` is exactly the show file of issue #55. It is checked against
    ``tara_core.show.ShowContainer`` before it is written, so nothing in the
    vault disagrees with ``schemas/show_v1.json``. A built show that fails the
    schema is not stored (the caller gets a warning); the rest of the project
    still saves, because the trajectory can be rebuilt from the scenes and a
    bad one must not block saving the design.
  * ``document`` is the Studio's command-layer document of issue #100
    (``scenes``, ``dronesList``, plus rate and home position), stored as the
    web app sends it. show_v1 describes waypoints, not scenes, so there is no
    #55 schema for it yet; ``format_version`` is what a migration keys on when
    there is one.

Save history (``versions``) is computed by the web app's projectVersions.js
and stored as part of the project. This layer bounds it, it does not
re-implement its rules.

Single writer: ``revision`` goes up by one on every save, and a save must name
the revision it was based on, so a stale browser gets a conflict instead of
silently overwriting newer work. Writes are serialised within one server
process; running several Studio backends over the same vault is not supported.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import tomllib
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from tara_core.show import SHOW_FORMAT_VERSION, ShowContainer

logger = logging.getLogger("TaramandalStudio")

PROJECT_FORMAT = "tara-project"
#: Bumped when project.json changes incompatibly.
PROJECT_FORMAT_VERSION = 1

#: Save-history entries kept per project. Mirrors MAX_VERSIONS in
#: tara-studio-web/src/engine/projectVersions.js, kept in sync by hand.
MAX_VERSIONS = 20

VAULT_DIR_ENV = "STUDIO_VAULT_DIR"
DEFAULT_VAULT_DIR = "~/.local/share/taramandal/projects"

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TOML = REPO_ROOT / "tara.toml"

PROJECT_FILE = "project.json"
SHOW_FILE = "show.json"
TRASH_DIR = ".trash"

#: Schema errors reported back for a rejected show; a bad show can have one
#: per waypoint.
MAX_REPORTED_SHOW_ERRORS = 20

# A project id is a directory name, so it is restricted to characters that
# cannot leave the vault or hide a directory.
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# Fields of project.json that are bookkeeping, not part of the project a
# client sees.
_INTERNAL_FIELDS = ("show_sha256", "imported_fingerprint")

_LOCK = threading.RLock()


class ProjectStoreError(Exception):
    """Base class for every failure this module raises."""


class ProjectNotFound(ProjectStoreError):
    pass


class ProjectExists(ProjectStoreError):
    pass


class InvalidProject(ProjectStoreError):
    """The id or content cannot be stored."""


class ProjectUnreadable(ProjectStoreError):
    """project.json exists but is corrupt, or written by a newer Studio."""


class RevisionConflict(ProjectStoreError):
    """The save was based on a revision that is no longer the latest."""

    def __init__(self, project_id: str, current_revision: int, base_revision: int):
        super().__init__(
            f"project {project_id} is at revision {current_revision}, "
            f"but the save was based on revision {base_revision}"
        )
        self.current_revision = current_revision
        self.base_revision = base_revision


@dataclass(frozen=True)
class Saved:
    """A stored project and anything the caller should be told about the save."""

    project: dict
    warnings: list[dict] = field(default_factory=list)


def vault_dir(toml_path: Path | None = None, environ: Mapping[str, str] = os.environ) -> Path:
    """Where projects are kept: STUDIO_VAULT_DIR, then tara.toml's
    ``[studio].vault_dir``, then a per-user default outside the checkout."""
    toml_path = DEFAULT_TOML if toml_path is None else toml_path
    raw = environ.get(VAULT_DIR_ENV)
    if not raw and Path(toml_path).exists():
        raw = tomllib.loads(Path(toml_path).read_text()).get("studio", {}).get("vault_dir")
    return Path(raw or DEFAULT_VAULT_DIR).expanduser()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _dumps(payload: Any) -> bytes:
    # allow_nan=False: NaN is not JSON, and a file holding one would not load
    # in the browser.
    try:
        return json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except ValueError as exc:
        raise InvalidProject(f"project is not valid JSON: {exc}") from exc


def _write_atomic(path: Path, blob: bytes) -> None:
    """Write-to-temp-then-replace, so a crash mid-write never leaves half a file."""
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _conforming_show(show: dict | None) -> tuple[dict | None, list[dict]]:
    """The show stamped with its format version, or None plus a warning when it
    does not match show_v1."""
    if show is None:
        return None, []
    data = {"version": SHOW_FORMAT_VERSION, **show}
    try:
        ShowContainer.model_validate(data)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False, include_context=False)
        return None, [{
            "code": "show_schema",
            "message": (
                "The built trajectory does not match the show schema (show_v1) and was not "
                "stored. The rest of the project was saved; rebuild the show and save again."
            ),
            "error_count": len(errors),
            "errors": errors[:MAX_REPORTED_SHOW_ERRORS],
        }]
    return data, []


def _import_fingerprint(name: str, document: dict, versions: list[dict]) -> str:
    canonical = json.dumps(
        {"name": name, "document": document, "versions": versions},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProjectStore:
    """The projects under one vault directory."""

    def __init__(self, root: Path | str, now: Callable[[], datetime] = _utc_now):
        self.root = Path(root)
        self._now = now

    # ---------------------------------------------------------------- read

    def list(self) -> list[dict]:
        """Every readable project, oldest first, without its built show.

        A corrupt project is logged and left out rather than failing the
        listing: one bad file must not hide the rest of the vault.
        """
        with _LOCK:
            projects = [self._public(meta) for meta in self._iter_meta()]
        return sorted(projects, key=lambda p: (p.get("created_at", ""), p["id"]))

    def get(self, project_id: str) -> dict:
        """One project including its built show (``show`` is None if it has none)."""
        with _LOCK:
            meta = self._read_meta(project_id)
            show = self._read_show(meta)
        project = self._public(meta)
        project["has_show"] = show is not None
        project["show"] = show
        return project

    # --------------------------------------------------------------- write

    def create(
        self,
        *,
        name: str,
        document: dict,
        show: dict | None = None,
        validation: dict | None = None,
        versions: list[dict] | None = None,
        project_id: str | None = None,
    ) -> Saved:
        """Stores a new project at revision 1.

        Raises:
            ProjectExists: ``project_id`` is already taken.
        """
        with _LOCK:
            return self._create(project_id, name, document, show, validation, versions or [], {})

    def save(
        self,
        project_id: str,
        *,
        base_revision: int,
        name: str,
        document: dict,
        show: dict | None = None,
        validation: dict | None = None,
        versions: list[dict] | None = None,
    ) -> Saved:
        """Replaces a project's content and bumps its revision.

        Raises:
            ProjectNotFound: no such project.
            RevisionConflict: the project was saved by someone else since
                ``base_revision``; nothing is written.
        """
        with _LOCK:
            meta = self._read_meta(project_id)
            if meta["revision"] != base_revision:
                raise RevisionConflict(project_id, meta["revision"], base_revision)
            self._check_versions(versions or [])
            meta.update(
                name=name,
                document=document,
                validation=validation,
                versions=versions or [],
                revision=meta["revision"] + 1,
                updated_at=self._stamp(),
            )
            return self._commit(meta, show)

    def delete(self, project_id: str) -> None:
        """Moves a project to the vault's .trash directory.

        Raises:
            ProjectNotFound: no such project.
        """
        with _LOCK:
            directory = self._dir(project_id)
            if not (directory / PROJECT_FILE).is_file():
                raise ProjectNotFound(project_id)
            trash = self.root / TRASH_DIR
            trash.mkdir(parents=True, exist_ok=True)
            when = self._now().strftime("%Y%m%dT%H%M%S%fZ")
            shutil.move(str(directory), str(trash / f"{project_id}.{when}"))

    def import_project(
        self,
        *,
        source_id: str | None,
        name: str,
        document: dict,
        show: dict | None = None,
        validation: dict | None = None,
        versions: list[dict] | None = None,
    ) -> tuple[Saved, str]:
        """Brings in a project from a browser's old localStorage vault.

        Safe to repeat: importing the same content twice stores it once. A
        project whose id is taken by different content is kept under a new id
        rather than overwriting or being dropped, since every browser's first
        project was called ``default-proj``.

        Returns:
            The stored project and ``"created"``, ``"renamed"`` (stored under
            a different id) or ``"skipped"`` (already imported).
        """
        versions = versions or []
        fingerprint = _import_fingerprint(name, document, versions)
        with _LOCK:
            for meta in self._iter_meta():
                if meta.get("imported_fingerprint") == fingerprint:
                    return Saved(self._public(meta)), "skipped"

            status = "created"
            project_id = source_id if isinstance(source_id, str) and _ID_RE.fullmatch(source_id) else None
            if project_id is not None and self._exists(project_id):
                project_id = f"{project_id[:55]}-{fingerprint[:8]}"
                status = "renamed"
                if self._exists(project_id):
                    project_id = None
            saved = self._create(
                project_id, name, document, show, validation, versions,
                {"imported_fingerprint": fingerprint},
            )
            return saved, status

    # ------------------------------------------------------------ internals

    def _dir(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or not _ID_RE.fullmatch(project_id):
            raise InvalidProject(
                "a project id is 1-64 letters, digits, '-' or '_', starting with a letter or digit"
            )
        return self.root / project_id

    def _exists(self, project_id: str) -> bool:
        return (self._dir(project_id) / PROJECT_FILE).is_file()

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _check_versions(versions: list[dict]) -> None:
        if len(versions) > MAX_VERSIONS:
            raise InvalidProject(f"a project keeps at most {MAX_VERSIONS} saved versions, got {len(versions)}")

    def _iter_meta(self) -> Iterator[dict]:
        if not self.root.is_dir():
            return
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir() or not _ID_RE.fullmatch(entry.name):
                continue
            try:
                yield self._read_meta(entry.name)
            except ProjectNotFound:
                continue
            except ProjectStoreError as exc:
                logger.warning("Skipping unreadable project %s: %s", entry.name, exc)

    def _read_meta(self, project_id: str) -> dict:
        path = self._dir(project_id) / PROJECT_FILE
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise ProjectNotFound(project_id) from None
        try:
            meta = json.loads(raw)
        except ValueError as exc:
            raise ProjectUnreadable(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(meta, dict) or meta.get("format") != PROJECT_FORMAT:
            raise ProjectUnreadable(f"{path} is not a {PROJECT_FORMAT} file")
        version = meta.get("format_version")
        if not isinstance(version, int) or version > PROJECT_FORMAT_VERSION:
            raise ProjectUnreadable(
                f"{path} has format_version {version!r}; this Studio reads up to {PROJECT_FORMAT_VERSION}"
            )
        if not isinstance(meta.get("revision"), int):
            raise ProjectUnreadable(f"{path} has no revision")
        # The directory is the identity: a project copied to a new directory
        # by hand is a new project.
        meta["id"] = project_id
        return meta

    def _read_show(self, meta: dict) -> dict | None:
        """The built show, or None when there is none or the file on disk is
        not the one this project.json was saved with."""
        expected = meta.get("show_sha256")
        if not expected:
            return None
        path = self._dir(meta["id"]) / SHOW_FILE
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        if hashlib.sha256(blob).hexdigest() != expected:
            logger.warning("Ignoring %s: it does not match the project it is stored with", path)
            return None
        try:
            return json.loads(blob)
        except ValueError:
            return None

    def _public(self, meta: dict) -> dict:
        project = {k: v for k, v in meta.items() if k not in _INTERNAL_FIELDS}
        project["has_show"] = bool(meta.get("show_sha256")) and (self._dir(meta["id"]) / SHOW_FILE).is_file()
        return project

    def _create(
        self,
        project_id: str | None,
        name: str,
        document: dict,
        show: dict | None,
        validation: dict | None,
        versions: list[dict],
        extra: dict,
    ) -> Saved:
        if project_id is None:
            project_id = f"proj-{uuid.uuid4().hex[:12]}"
        if self._exists(project_id):
            raise ProjectExists(project_id)
        self._check_versions(versions)
        now = self._stamp()
        meta = {
            "format": PROJECT_FORMAT,
            "format_version": PROJECT_FORMAT_VERSION,
            "id": project_id,
            "name": name,
            "revision": 1,
            "created_at": now,
            "updated_at": now,
            "document": document,
            "validation": validation,
            "versions": versions,
            **extra,
        }
        return self._commit(meta, show)

    def _commit(self, meta: dict, show: dict | None) -> Saved:
        show, warnings = _conforming_show(show)
        show_blob = _dumps(show) if show is not None else None
        meta["show_sha256"] = hashlib.sha256(show_blob).hexdigest() if show_blob is not None else None
        meta_blob = _dumps(meta)

        directory = self._dir(meta["id"])
        directory.mkdir(parents=True, exist_ok=True)
        # project.json is the commit point and names the show it goes with by
        # hash, so a crash between the two writes leaves a project whose show
        # is treated as absent, never one paired with the wrong trajectory.
        if show_blob is not None:
            _write_atomic(directory / SHOW_FILE, show_blob)
        _write_atomic(directory / PROJECT_FILE, meta_blob)
        if show_blob is None:
            (directory / SHOW_FILE).unlink(missing_ok=True)
        return Saved(self._public(meta), warnings)
