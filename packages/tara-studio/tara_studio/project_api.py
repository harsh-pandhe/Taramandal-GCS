"""HTTP API of the Project Vault (issue #101). Storage is project_store.py.

    GET    /api/projects            every project, without its built show
    POST   /api/projects            create
    GET    /api/projects/{id}       one project with its built show
    PUT    /api/projects/{id}       save (needs the revision it was based on)
    DELETE /api/projects/{id}       move to the vault's trash
    POST   /api/projects/import     bring in a project from a browser's old
                                    localStorage vault; safe to repeat

Errors carry ``detail = {"code", "message", ...}`` so the web app can tell a
stale save (409 revision_conflict) from a missing project (404) without
parsing prose.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from tara_core import MAX_FLEET_SIZE
from tara_studio.project_store import (
    MAX_VERSIONS,
    InvalidProject,
    ProjectExists,
    ProjectNotFound,
    ProjectStore,
    ProjectStoreError,
    RevisionConflict,
    Saved,
    vault_dir,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


def get_store() -> ProjectStore:
    """The vault, resolved per request so STUDIO_VAULT_DIR is read when used."""
    return ProjectStore(vault_dir())


class ProjectDocument(BaseModel):
    """The Studio's editable document (issue #100's command layer), stored as sent.

    Only the two lists every part of the Studio relies on are checked. The
    rest (rate, home position, whatever a later command adds) passes through,
    because the web app owns this shape and show_v1 (issue #55) does not
    describe it.
    """

    model_config = ConfigDict(extra="allow")

    scenes: list[dict[str, Any]] = Field(..., description="Scene keyframes, in show order")
    dronesList: list[dict[str, Any]] = Field(..., max_length=MAX_FLEET_SIZE, description="The fleet")


class ProjectVersion(BaseModel):
    """One save-history entry, as projectVersions.js builds it."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., min_length=1)
    savedAt: str = Field(..., min_length=1)


class ProjectBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    document: ProjectDocument
    show: dict[str, Any] | None = Field(
        None, description="The built trajectory, a show_v1 document (issue #55), or null if not built"
    )
    validation: dict[str, Any] | None = Field(None, description="Safety report of the built show")
    versions: list[ProjectVersion] = Field(
        default_factory=list, max_length=MAX_VERSIONS, description="Save history, newest first"
    )

    def content(self) -> dict:
        return {
            "name": self.name,
            "document": self.document.model_dump(),
            "show": self.show,
            "validation": self.validation,
            "versions": [v.model_dump() for v in self.versions],
        }


class CreateProject(ProjectBody):
    id: str | None = Field(None, description="Id to create under; generated when omitted")


class SaveProject(ProjectBody):
    base_revision: int = Field(..., ge=1, description="The revision this save was based on")


class ImportProject(ProjectBody):
    id: str | None = Field(None, description="The project's id in the browser it comes from")


@contextmanager
def _store_errors() -> Iterator[None]:
    try:
        yield
    except ProjectNotFound as exc:
        raise HTTPException(404, {"code": "project_not_found", "message": f"No project {exc} in the vault."})
    except ProjectExists as exc:
        raise HTTPException(409, {"code": "project_exists", "message": f"Project {exc} already exists."})
    except RevisionConflict as exc:
        raise HTTPException(409, {
            "code": "revision_conflict",
            "message": "This project was saved somewhere else since it was opened here.",
            "current_revision": exc.current_revision,
        })
    except InvalidProject as exc:
        raise HTTPException(422, {"code": "invalid_project", "message": str(exc)})
    except ProjectStoreError as exc:
        raise HTTPException(500, {"code": "project_unreadable", "message": str(exc)})
    except OSError as exc:
        # Disk full, vault directory not writable: the vault's problem, and
        # worth telling the designer rather than failing as a blank 500.
        raise HTTPException(503, {"code": "vault_unavailable", "message": f"Project Vault storage failed: {exc}"})


def _saved(saved: Saved, **extra: Any) -> dict:
    return {"project": saved.project, "warnings": saved.warnings, **extra}


@router.get("")
def list_projects(store: ProjectStore = Depends(get_store)):
    with _store_errors():
        return {"projects": store.list()}


@router.post("", status_code=201)
def create_project(req: CreateProject, store: ProjectStore = Depends(get_store)):
    with _store_errors():
        return _saved(store.create(project_id=req.id, **req.content()))


@router.post("/import")
def import_project(req: ImportProject, store: ProjectStore = Depends(get_store)):
    with _store_errors():
        saved, status = store.import_project(source_id=req.id, **req.content())
        return _saved(saved, status=status)


@router.get("/{project_id}")
def get_project(project_id: str, store: ProjectStore = Depends(get_store)):
    with _store_errors():
        return {"project": store.get(project_id)}


@router.put("/{project_id}")
def save_project(project_id: str, req: SaveProject, store: ProjectStore = Depends(get_store)):
    with _store_errors():
        return _saved(store.save(project_id, base_revision=req.base_revision, **req.content()))


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, store: ProjectStore = Depends(get_store)):
    with _store_errors():
        store.delete(project_id)
    return Response(status_code=204)
