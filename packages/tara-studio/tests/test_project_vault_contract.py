"""Issue #101: the web app's tests run against a stand-in vault; this pins it
to the real one.

The Studio web tests (Vitest, Playwright) do not start the Python backend.
They use ``tara-studio-web/src/engine/__fixtures__/fakeVaultServer.js``, an
in-memory copy of the /api/projects API. A copy can drift, so both sides are
held to one recording:

* ``vault-contract.json`` is the requests below and what the live FastAPI app
  answers. This module fails if the server no longer answers that way;
* ``fakeVaultServer.test.js`` replays the same requests against the fake and
  fails if it answers differently.

Change the API, and one of the two suites fails until the other side follows.
Only what a recording cannot hold steady is masked: timestamps, the content
hash in a renamed import's id, and error prose (the ``code`` is the contract).

To refresh the fixture after an intentional API change:

    PYTHONPATH=packages/tara-core:packages/tara-studio \\
        python packages/tara-studio/tests/test_project_vault_contract.py
"""

import json
import os
import re
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from tara_studio import project_store
from tara_studio.server import app

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tara-studio-web" / "src" / "engine" / "__fixtures__" / "vault-contract.json"
)


def _doc(*scene_ids: str) -> dict:
    return {
        "scenes": [{"id": s, "name": s.upper()} for s in scene_ids],
        "dronesList": [{"id": 0, "name": "Drone 1"}, {"id": 1, "name": "Drone 2"}],
        "rate": 4,
        "homeLat": 12.9716,
        "homeLon": 77.5946,
    }


def _version(n: int, *scene_ids: str) -> dict:
    return {"id": f"v-{n}", "savedAt": f"2026-10-06T10:0{n}:00.000Z", "label": "Saved", **_doc(*scene_ids)}


def _show(yaw: float = 0.0) -> dict:
    return {
        "drones": [
            {"id": i, "waypoints": [
                {"time": t, "x": 0.0, "y": 2.0 * i, "z": -5.0, "yaw": yaw} for t in (0.0, 1.0)
            ]}
            for i in (0, 1)
        ],
        "_duration": 1.0,
    }


#: (name, method, path, body). Order matters: each step sees the vault the
#: earlier ones left.
STEPS = [
    ("list an empty vault", "GET", "/api/projects", None),
    ("create", "POST", "/api/projects", {"id": "diwali", "name": "Diwali", "document": _doc("s1")}),
    ("create over an existing id", "POST", "/api/projects", {"id": "diwali", "name": "Again", "document": _doc("s1")}),
    ("create with an unusable id", "POST", "/api/projects", {"id": "bad.id", "name": "x", "document": _doc("s1")}),
    ("create without a name", "POST", "/api/projects", {"name": "", "document": _doc("s1")}),
    ("get", "GET", "/api/projects/diwali", None),
    ("get a missing project", "GET", "/api/projects/ghost", None),
    ("save with a built show and history", "PUT", "/api/projects/diwali", {
        "base_revision": 1, "name": "Diwali 2026", "document": _doc("s1", "s2"),
        "show": _show(), "validation": {"status": "PASSED"}, "versions": [_version(1, "s1", "s2")],
    }),
    ("get with the built show", "GET", "/api/projects/diwali", None),
    ("list", "GET", "/api/projects", None),
    ("save from a stale revision", "PUT", "/api/projects/diwali", {
        "base_revision": 1, "name": "Stale", "document": _doc("s1"),
    }),
    ("save a show that breaks show_v1", "PUT", "/api/projects/diwali", {
        "base_revision": 2, "name": "Diwali 2026", "document": _doc("s1", "s2", "s3"),
        "show": _show(yaw=400.0), "versions": [_version(2, "s1", "s2", "s3"), _version(1, "s1", "s2")],
    }),
    ("save a missing project", "PUT", "/api/projects/ghost", {
        "base_revision": 1, "name": "Ghost", "document": _doc("s1"),
    }),
    ("save without a base revision", "PUT", "/api/projects/diwali", {"name": "x", "document": _doc("s1")}),
    ("save with too much history", "PUT", "/api/projects/diwali", {
        "base_revision": 3, "name": "x", "document": _doc("s1"),
        "versions": [{"id": f"v-{n}", "savedAt": "2026-10-06T10:00:00.000Z"} for n in range(21)],
    }),
    ("import a browser's project", "POST", "/api/projects/import", {
        "id": "default-proj", "name": "Untitled Show", "document": _doc("laptop"), "versions": [_version(1, "laptop")],
    }),
    ("import it again", "POST", "/api/projects/import", {
        "id": "default-proj", "name": "Untitled Show", "document": _doc("laptop"), "versions": [_version(1, "laptop")],
    }),
    ("import another browser's project with the same id", "POST", "/api/projects/import", {
        "id": "default-proj", "name": "Untitled Show", "document": _doc("desktop"),
    }),
    ("delete", "DELETE", "/api/projects/diwali", None),
    ("delete again", "DELETE", "/api/projects/diwali", None),
    ("list after all of it", "GET", "/api/projects", None),
]

_RENAMED_ID = re.compile(r"^(default-proj)-[0-9a-f]{8}$")


def _mask(value):
    """Replaces what differs run to run (see the module docstring)."""
    if isinstance(value, list):
        return [_mask(v) for v in value]
    if not isinstance(value, dict):
        return value
    out = {}
    for key, v in value.items():
        if key in ("created_at", "updated_at"):
            out[key] = "<time>"
        elif key == "id" and isinstance(v, str) and _RENAMED_ID.match(v):
            out[key] = _RENAMED_ID.sub(r"\1-<fingerprint>", v)
        elif key == "warnings":
            out[key] = [{"code": w["code"]} for w in v]
        elif key == "detail":
            # FastAPI's own request-validation errors are a list of pydantic
            # messages; the vault's errors are {"code", "message", ...}.
            out[key] = "<validation>" if isinstance(v, list) else {k: x for k, x in v.items() if k != "message"}
        else:
            out[key] = _mask(v)
    return out


def record() -> dict:
    """Runs STEPS against the live app over an empty vault."""
    previous = os.environ.get(project_store.VAULT_DIR_ENV)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ[project_store.VAULT_DIR_ENV] = str(Path(tmp) / "vault")
        try:
            client = TestClient(app)
            steps = []
            for name, method, path, body in STEPS:
                res = client.request(method, path, json=body) if body is not None else client.request(method, path)
                steps.append({
                    "name": name,
                    "request": {"method": method, "path": path, "body": body},
                    "response": {
                        "status": res.status_code,
                        "body": _mask(res.json()) if res.content else None,
                    },
                })
        finally:
            if previous is None:
                os.environ.pop(project_store.VAULT_DIR_ENV, None)
            else:
                os.environ[project_store.VAULT_DIR_ENV] = previous
    return {"steps": steps}


def test_the_live_api_answers_exactly_as_the_recording_the_web_tests_rely_on():
    recorded = json.loads(FIXTURE.read_text())
    live = record()
    assert [s["name"] for s in live["steps"]] == [s["name"] for s in recorded["steps"]]
    for live_step, recorded_step in zip(live["steps"], recorded["steps"]):
        assert live_step == recorded_step, f"the vault API changed at step: {live_step['name']}"


def test_the_recording_covers_every_status_the_web_app_handles():
    statuses = {s["response"]["status"] for s in json.loads(FIXTURE.read_text())["steps"]}
    assert statuses == {200, 201, 204, 404, 409, 422}


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(record(), indent=2) + "\n")
    print(f"wrote {FIXTURE}")
