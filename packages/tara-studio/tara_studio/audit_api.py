"""HTTP API of the AI audit log (issues #94, #89). Storage is audit_log.py.

    POST /api/projects/{id}/audit              append one entry
    GET  /api/projects/{id}/audit?limit=N      the latest N, newest first (default 100, max 500),
                                               with total, full, chainOk, torn, corruptLines

There is deliberately no PUT, PATCH or DELETE: an entry cannot be edited or
removed through the API, and audit_log.py has no function that would do it.

The project API has no request body cap. This route has one: a valid entry is
under 4 KB, so a body over ``MAX_BODY_BYTES`` is refused (413) while it is being
read, before it is parsed or kept whole.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from tara_studio.audit_log import DEFAULT_LIMIT, MAX_LIMIT, AuditEntryIn, AuditFull, AuditLog
from tara_studio.project_api import _store_errors, get_store
from tara_studio.project_store import ProjectStore

router = APIRouter(prefix="/api/projects/{project_id}/audit", tags=["audit"])

#: Largest request body accepted. An entry is under 4 KB; the rest is slack for whitespace.
MAX_BODY_BYTES = 64 * 1024


def _too_large() -> HTTPException:
    return HTTPException(413, {
        "code": "body_too_large",
        "message": f"An audit entry request is at most {MAX_BODY_BYTES} bytes.",
    })


async def _read_entry(request: Request) -> AuditEntryIn:
    """The request body as a validated entry, reading at most MAX_BODY_BYTES."""
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(415, {"code": "unsupported_media_type", "message": "Send the entry as application/json."})
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _too_large()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise _too_large()
    try:
        data: Any = json.loads(bytes(body))
    except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested too deeply
        raise HTTPException(422, {"code": "invalid_entry", "message": "The body is not valid JSON."}) from None
    try:
        return AuditEntryIn.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(422, {
            "code": "invalid_entry",
            "message": "The audit entry is not valid.",
            "errors": exc.errors(include_url=False, include_context=False, include_input=False),
        }) from None


def _storage_failed(exc: OSError) -> HTTPException:
    # The vault convention is a 503 (project_api); the OS message names a path on
    # this machine, so it is left out of the response.
    return HTTPException(503, {
        "code": "vault_unavailable",
        "message": "Project Vault storage failed: the audit log could not be read or written.",
    })


def _append(store: ProjectStore, project_id: str, entry: AuditEntryIn) -> dict:
    with _store_errors():
        try:
            return {"entry": AuditLog(store).append(project_id, entry)}
        except AuditFull:
            raise HTTPException(409, {
                "code": "audit_full",
                "message": "The audit log for this project is full and records nothing more.",
            }) from None
        except OSError as exc:
            raise _storage_failed(exc) from None


@router.post("", status_code=201)
async def append_entry(project_id: str, request: Request, store: ProjectStore = Depends(get_store)):
    entry = await _read_entry(request)
    return await run_in_threadpool(_append, store, project_id, entry)


@router.get("")
def read_entries(
    project_id: str,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    store: ProjectStore = Depends(get_store),
):
    with _store_errors():
        try:
            return AuditLog(store).read(project_id, limit)
        except OSError as exc:
            raise _storage_failed(exc) from None
