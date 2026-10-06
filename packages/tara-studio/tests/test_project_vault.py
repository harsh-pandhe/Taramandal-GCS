"""Issue #101: the Project Vault keeps Studio projects on the backend.

Driven through the HTTP API the web app uses, against a vault in a temp
directory, and checked on disk where the claim is about what is stored.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tara_core.show import SHOW_FORMAT_VERSION, Show
from tara_studio import project_store
from tara_studio.generator import generate_circle
from tara_studio.project_store import MAX_VERSIONS, ProjectStore, RevisionConflict, vault_dir
from tara_studio.server import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def vault(tmp_path, monkeypatch):
    """A fresh vault per test; never the developer's own projects or tara.toml."""
    root = tmp_path / "vault"
    monkeypatch.setenv(project_store.VAULT_DIR_ENV, str(root))
    monkeypatch.setattr(project_store, "DEFAULT_TOML", tmp_path / "tara.toml")
    return root


def scene(scene_id: str, name: str = "Scene") -> dict:
    return {"id": scene_id, "name": name, "shapeType": "text", "shapePayload": name, "holdDurationSeconds": 6}


def document(*scene_ids: str, drones: int = 3) -> dict:
    return {
        "scenes": [scene(s) for s in scene_ids or ("s1",)],
        "dronesList": [{"id": i, "name": f"Drone {i + 1}", "color": "#38bdf8"} for i in range(drones)],
        "rate": 4,
        "homeLat": 12.9716,
        "homeLon": 77.5946,
    }


def version(n: int, doc: dict, label: str = "Saved") -> dict:
    """A save-history entry shaped like projectVersions.js addVersion() makes it."""
    return {"id": f"v-{n}", "savedAt": f"2026-10-06T10:{n:02d}:00.000Z", "label": label, **doc}


def create(project_id: str | None = "diwali", name: str = "Diwali", **body) -> dict:
    payload = {"name": name, "document": document(), **body}
    if project_id is not None:
        payload["id"] = project_id
    res = client.post("/api/projects", json=payload)
    assert res.status_code == 201, res.text
    return res.json()["project"]


def save(project_id: str, base_revision: int, **body):
    payload = {"name": "Diwali", "document": document(), "base_revision": base_revision, **body}
    return client.put(f"/api/projects/{project_id}", json=payload)


def get(project_id: str) -> dict:
    res = client.get(f"/api/projects/{project_id}")
    assert res.status_code == 200, res.text
    return res.json()["project"]


def listed() -> list[dict]:
    return client.get("/api/projects").json()["projects"]


def error_code(res) -> str:
    return res.json()["detail"]["code"]


class TestCreateAndRead:
    def test_a_new_vault_is_empty_and_reading_it_creates_nothing(self, vault):
        assert listed() == []
        assert not vault.exists()

    def test_a_created_project_is_listed_and_readable(self):
        created = create(document=document("s1", "s2"))
        assert created["id"] == "diwali"
        assert created["revision"] == 1
        assert created["format"] == "tara-project"
        assert created["created_at"] == created["updated_at"]
        assert created["has_show"] is False

        assert [p["id"] for p in listed()] == ["diwali"]
        project = get("diwali")
        assert project["name"] == "Diwali"
        assert project["document"] == document("s1", "s2")
        assert project["show"] is None
        assert project["versions"] == []

    def test_the_document_is_stored_as_sent_including_fields_the_server_does_not_know(self):
        doc = {**document(), "homeLat": "12.97", "ledTrack": [{"t": 1.5, "color": "#fff"}]}
        create(document=doc)
        assert get("diwali")["document"] == doc

    def test_an_id_is_generated_when_none_is_given(self):
        first, second = create(project_id=None), create(project_id=None)
        assert first["id"] != second["id"]
        assert {p["id"] for p in listed()} == {first["id"], second["id"]}

    def test_projects_are_listed_oldest_first_without_their_built_show(self):
        create("b-second-alphabetically")
        create("a-first-alphabetically", show=generate_circle(2, 30.0, 2.0))
        projects = listed()
        assert [p["id"] for p in projects] == ["b-second-alphabetically", "a-first-alphabetically"]
        assert "show" not in projects[1]
        assert projects[1]["has_show"] is True

    def test_creating_over_an_existing_id_is_refused_and_changes_nothing(self):
        create(name="Original")
        res = client.post("/api/projects", json={"id": "diwali", "name": "Impostor", "document": document()})
        assert res.status_code == 409
        assert error_code(res) == "project_exists"
        assert get("diwali")["name"] == "Original"

    def test_an_unknown_project_is_a_404(self):
        res = client.get("/api/projects/never-saved")
        assert res.status_code == 404
        assert error_code(res) == "project_not_found"

    @pytest.mark.parametrize("bad_id", ["..", ".trash", "has.dot", "-leading-dash", "x" * 65, "sp ace"])
    def test_an_id_that_is_not_a_safe_directory_name_is_refused(self, bad_id, vault):
        res = client.post("/api/projects", json={"id": bad_id, "name": "x", "document": document()})
        assert res.status_code == 422
        assert error_code(res) == "invalid_project"
        assert not vault.exists()

    @pytest.mark.parametrize("bad_id", ["..", "../outside", ".trash", "a/b", ""])
    def test_no_id_reaches_outside_the_vault(self, bad_id, vault, tmp_path):
        """Checked on the store, because an HTTP client normalises `..` away
        before the server ever sees it."""
        (tmp_path / "outside").mkdir()
        (tmp_path / "outside" / "project.json").write_text('{"format": "tara-project"}')
        store = ProjectStore(vault)
        for attempt in (
            lambda: store.get(bad_id),
            lambda: store.delete(bad_id),
            lambda: store.create(project_id=bad_id, name="x", document=document()),
            lambda: store.save(bad_id, base_revision=1, name="x", document=document()),
        ):
            with pytest.raises(project_store.InvalidProject):
                attempt()
        assert (tmp_path / "outside" / "project.json").is_file()

    @pytest.mark.parametrize("body", [
        {"document": document()},                                             # no name
        {"name": "", "document": document()},                                 # empty name
        {"name": "x"},                                                        # no document
        {"name": "x", "document": {"scenes": "nope", "dronesList": []}},      # scenes not a list
        {"name": "x", "document": {"scenes": []}},                            # no fleet
        {"name": "x", "document": document(), "show": [1, 2, 3]},             # show not an object
    ])
    def test_a_malformed_project_is_refused(self, body):
        assert client.post("/api/projects", json=body).status_code == 422
        assert listed() == []

    def test_nan_cannot_be_stored(self):
        body = '{"id": "diwali", "name": "x", "document": {"scenes": [], "dronesList": [], "rate": NaN}}'
        res = client.post("/api/projects", content=body, headers={"Content-Type": "application/json"})
        assert res.status_code == 422
        assert listed() == []


class TestSave:
    def test_a_save_replaces_the_content_and_bumps_the_revision(self):
        created = create()
        res = save("diwali", 1, name="Diwali 2026", document=document("s1", "s2", "s3"))
        assert res.status_code == 200
        saved = res.json()["project"]
        assert saved["revision"] == 2
        assert saved["created_at"] == created["created_at"]

        project = get("diwali")
        assert project["name"] == "Diwali 2026"
        assert [s["id"] for s in project["document"]["scenes"]] == ["s1", "s2", "s3"]
        assert project["revision"] == 2

    def test_updated_at_moves_and_created_at_does_not(self, vault):
        clock = iter(datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc) + timedelta(minutes=m) for m in range(9))
        store = ProjectStore(vault, now=lambda: next(clock))
        store.create(project_id="diwali", name="Diwali", document=document())
        saved = store.save("diwali", base_revision=1, name="Diwali", document=document()).project
        assert saved["created_at"] == "2026-10-06T10:00:00.000Z"
        assert saved["updated_at"] == "2026-10-06T10:01:00.000Z"

    def test_a_save_based_on_a_stale_revision_is_refused_and_writes_nothing(self):
        create()
        assert save("diwali", 1, name="From browser A").status_code == 200

        res = save("diwali", 1, name="From browser B, which never saw A's save")
        assert res.status_code == 409
        assert error_code(res) == "revision_conflict"
        assert res.json()["detail"]["current_revision"] == 2

        project = get("diwali")
        assert project["name"] == "From browser A"
        assert project["revision"] == 2

    def test_the_store_raises_the_conflict_with_both_revisions(self, vault):
        store = ProjectStore(vault)
        store.create(project_id="diwali", name="Diwali", document=document())
        with pytest.raises(RevisionConflict) as exc:
            store.save("diwali", base_revision=7, name="Diwali", document=document())
        assert (exc.value.current_revision, exc.value.base_revision) == (1, 7)

    def test_saving_a_project_that_does_not_exist_is_a_404_not_a_create(self):
        res = save("never-created", 1)
        assert res.status_code == 404
        assert listed() == []

    def test_a_save_needs_the_revision_it_was_based_on(self):
        create()
        res = client.put("/api/projects/diwali", json={"name": "Diwali", "document": document()})
        assert res.status_code == 422
        assert get("diwali")["revision"] == 1


class TestBuiltShow:
    def test_the_built_show_round_trips_with_the_studios_own_fields(self):
        show = {**generate_circle(3, 30.0, 2.0), "_duration": 41.5, "_targetIdx": [[0, 1, 2]]}
        created = create(show=show, validation={"status": "PASSED", "metrics": {"min_distance_m": 2.0}})
        assert created["has_show"] is True

        project = get("diwali")
        assert project["show"] == {"version": SHOW_FORMAT_VERSION, **show}
        assert project["validation"] == {"status": "PASSED", "metrics": {"min_distance_m": 2.0}}

    def test_the_show_on_disk_is_a_show_v1_file(self, vault):
        """Issue #55's format, not a second one: the stored trajectory loads
        through the same strict gate the GCS upload path uses."""
        create(show=generate_circle(4, 30.0, 2.0))
        on_disk = json.loads((vault / "diwali" / "show.json").read_text())
        assert on_disk["version"] == SHOW_FORMAT_VERSION
        assert Show.validate_and_load(on_disk).num_drones == 4
        assert "show" not in json.loads((vault / "diwali" / "project.json").read_text())

    def test_a_show_that_breaks_the_schema_is_not_stored_but_the_project_still_saves(self, vault):
        create(show=generate_circle(2, 30.0, 2.0))
        bad = {"drones": [{"id": 0, "waypoints": [{"time": 0, "x": 0, "y": 0, "z": -5, "yaw": 400}]}]}

        res = save("diwali", 1, document=document("s1", "s2"), show=bad)
        assert res.status_code == 200
        body = res.json()
        assert body["project"]["has_show"] is False
        assert [w["code"] for w in body["warnings"]] == ["show_schema"]
        assert body["warnings"][0]["errors"][0]["loc"] == ["drones", 0, "waypoints", 0, "yaw"]

        project = get("diwali")
        assert project["show"] is None
        assert len(project["document"]["scenes"]) == 2
        assert not (vault / "diwali" / "show.json").exists()

    def test_saving_without_a_show_drops_the_previous_one(self, vault):
        create(show=generate_circle(2, 30.0, 2.0))
        assert save("diwali", 1, show=None).json()["project"]["has_show"] is False
        assert get("diwali")["show"] is None
        assert not (vault / "diwali" / "show.json").exists()

    def test_a_show_file_that_is_not_the_one_saved_with_the_project_is_ignored(self, vault):
        """E.g. a crash between the two writes, or a hand-edited file: the
        project opens without a built show rather than with the wrong one."""
        create(show=generate_circle(2, 30.0, 2.0))
        (vault / "diwali" / "show.json").write_text(json.dumps(generate_circle(5, 30.0, 2.0)))
        project = get("diwali")
        assert project["show"] is None
        assert project["has_show"] is False
        assert project["document"] == document()

    def test_a_missing_show_file_reads_as_not_built(self, vault):
        create(show=generate_circle(2, 30.0, 2.0))
        (vault / "diwali" / "show.json").unlink()
        assert get("diwali")["show"] is None
        assert listed()[0]["has_show"] is False


class TestSaveHistory:
    """Issue #100's save history is computed by the web app (projectVersions.js)
    and must come back from the vault exactly as it was sent."""

    def test_versions_round_trip_in_order_with_every_field(self):
        v1, v2 = version(1, document("s1")), version(2, document("s1", "s2"))
        create(versions=[v1])
        res = save("diwali", 1, document=document("s1", "s2"), versions=[v2, v1])
        assert res.json()["project"]["versions"] == [v2, v1]
        assert get("diwali")["versions"] == [v2, v1]
        assert listed()[0]["versions"] == [v2, v1]

    def test_restoring_an_earlier_version_is_a_save_that_keeps_the_whole_history(self):
        """What useProjects.restore sends: the head put back to a version, the
        replaced state kept as "Before restore", and the built show cleared."""
        v1, v2 = version(1, document("s1")), version(2, document("s1", "s2", "s3"))
        create(document=document("s1", "s2", "s3"), versions=[v2, v1], show=generate_circle(3, 30.0, 2.0))

        unsaved = document("s1", "s2", "s3", "s4")
        before = version(3, unsaved, label="Before restore")
        res = save("diwali", 1, document=document("s1"), versions=[before, v2, v1], show=None)
        assert res.status_code == 200

        project = get("diwali")
        assert [s["id"] for s in project["document"]["scenes"]] == ["s1"]
        assert [v["label"] for v in project["versions"]] == ["Before restore", "Saved", "Saved"]
        assert [s["id"] for s in project["versions"][0]["scenes"]] == ["s1", "s2", "s3", "s4"]
        assert project["show"] is None

    def test_history_is_bounded(self):
        create()
        full = [version(n, document(f"s{n}")) for n in range(MAX_VERSIONS)]
        assert save("diwali", 1, versions=full).status_code == 200
        assert len(get("diwali")["versions"]) == MAX_VERSIONS

        res = save("diwali", 2, versions=[version(99, document("s99")), *full])
        assert res.status_code == 422
        assert get("diwali")["revision"] == 2

    def test_a_version_without_an_id_is_refused(self):
        create()
        assert save("diwali", 1, versions=[{"savedAt": "2026-10-06T10:00:00Z"}]).status_code == 422


class TestDelete:
    def test_a_deleted_project_is_gone_from_the_api(self):
        create("keep")
        create("drop")
        assert client.delete("/api/projects/drop").status_code == 204
        assert [p["id"] for p in listed()] == ["keep"]
        assert client.get("/api/projects/drop").status_code == 404
        assert client.delete("/api/projects/drop").status_code == 404

    def test_a_deleted_project_is_kept_in_the_trash(self, vault):
        create("drop", name="Recoverable", show=generate_circle(2, 30.0, 2.0))
        client.delete("/api/projects/drop")
        (trashed,) = (vault / ".trash").iterdir()
        assert trashed.name.startswith("drop.")
        assert json.loads((trashed / "project.json").read_text())["name"] == "Recoverable"
        assert (trashed / "show.json").is_file()

    def test_the_id_can_be_used_again_after_a_delete(self):
        create("drop", name="First")
        client.delete("/api/projects/drop")
        assert create("drop", name="Second")["revision"] == 1
        assert get("drop")["name"] == "Second"


class TestImport:
    """Existing localStorage projects are brought in, not stranded."""

    @staticmethod
    def legacy(project_id="default-proj", name="Untitled Show", **body) -> dict:
        return {"id": project_id, "name": name, "document": document(), **body}

    def post(self, payload: dict) -> dict:
        res = client.post("/api/projects/import", json=payload)
        assert res.status_code == 200, res.text
        return res.json()

    def test_a_project_is_imported_under_its_own_id_with_history_and_show(self):
        v1 = version(1, document("s1"))
        body = self.post(self.legacy("proj-1759400000000", "Ganesh", versions=[v1], show=generate_circle(2, 30.0, 2.0)))
        assert body["status"] == "created"
        assert body["project"]["id"] == "proj-1759400000000"

        project = get("proj-1759400000000")
        assert project["name"] == "Ganesh"
        assert project["versions"] == [v1]
        assert project["show"]["version"] == SHOW_FORMAT_VERSION
        assert project["revision"] == 1

    def test_importing_the_same_project_again_stores_it_once(self):
        first = self.post(self.legacy())
        again = self.post(self.legacy())
        assert again["status"] == "skipped"
        assert again["project"]["id"] == first["project"]["id"]
        assert len(listed()) == 1

    def test_a_second_browsers_default_project_is_kept_beside_the_first(self):
        """Every browser's first project is `default-proj`. Different content
        under the same id is someone's work: neither overwritten nor dropped."""
        self.post(self.legacy(document=document("laptop-scene")))
        body = self.post(self.legacy(document=document("desktop-scene")))
        assert body["status"] == "renamed"
        new_id = body["project"]["id"]
        assert new_id.startswith("default-proj-")

        assert get("default-proj")["document"]["scenes"][0]["id"] == "laptop-scene"
        assert get(new_id)["document"]["scenes"][0]["id"] == "desktop-scene"

        assert self.post(self.legacy(document=document("desktop-scene")))["status"] == "skipped"
        assert len(listed()) == 2

    def test_an_edited_import_is_still_recognised_as_imported(self):
        self.post(self.legacy())
        assert save("default-proj", 1, name="Renamed since", document=document("s1", "s2")).status_code == 200
        assert self.post(self.legacy())["status"] == "skipped"
        assert get("default-proj")["name"] == "Renamed since"

    def test_an_id_the_vault_cannot_use_gets_a_generated_one(self):
        body = self.post(self.legacy("proj 2026/10"))
        assert body["status"] == "created"
        assert get(body["project"]["id"])["name"] == "Untitled Show"

    def test_an_imported_show_that_breaks_the_schema_is_reported_and_the_project_kept(self):
        body = self.post(self.legacy(show={"drones": "not a list"}))
        assert [w["code"] for w in body["warnings"]] == ["show_schema"]
        assert get("default-proj")["show"] is None

    def test_bookkeeping_fields_are_not_part_of_the_project(self):
        project = self.post(self.legacy(show=generate_circle(2, 30.0, 2.0)))["project"]
        assert "imported_fingerprint" not in project
        assert "show_sha256" not in project
        assert "show_sha256" not in get("default-proj")


class TestSurvivesRestart:
    def test_a_fresh_server_process_serves_what_the_last_one_saved(self, vault):
        """The acceptance criterion, minus the browser: nothing a project needs
        lives in the server's memory."""
        v1 = version(1, document("s1"))
        create(document=document("s1"), versions=[v1], show=generate_circle(3, 30.0, 2.0))
        assert save("diwali", 1, name="Diwali final", document=document("s1", "s2"),
                    versions=[version(2, document("s1", "s2")), v1],
                    show=generate_circle(3, 30.0, 2.0)).status_code == 200

        script = (
            "import json\n"
            "from fastapi.testclient import TestClient\n"
            "from tara_studio.server import app\n"
            "c = TestClient(app)\n"
            "print(json.dumps({'list': c.get('/api/projects').json(),"
            " 'one': c.get('/api/projects/diwali').json(),"
            " 'save': c.put('/api/projects/diwali', json={'name': 'After restart', 'base_revision': 2,"
            " 'document': {'scenes': [], 'dronesList': []}}).status_code}))\n"
        )
        packages = Path(__file__).resolve().parents[2]
        env = {
            **os.environ,
            project_store.VAULT_DIR_ENV: str(vault),
            "PYTHONPATH": os.pathsep.join(str(packages / p) for p in ("tara-core", "tara-studio")),
        }
        out = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr
        seen = json.loads(out.stdout.strip().splitlines()[-1])

        assert [p["id"] for p in seen["list"]["projects"]] == ["diwali"]
        project = seen["one"]["project"]
        assert project["name"] == "Diwali final"
        assert project["revision"] == 2
        assert [v["id"] for v in project["versions"]] == ["v-2", "v-1"]
        assert len(project["show"]["drones"]) == 3
        # ... and the revision carried over, so the restarted server accepts the next save.
        assert seen["save"] == 200
        assert get("diwali")["name"] == "After restart"

    def test_a_new_store_on_the_same_directory_sees_the_same_projects(self, vault):
        ProjectStore(vault).create(project_id="diwali", name="Diwali", document=document())
        assert [p["id"] for p in ProjectStore(vault).list()] == ["diwali"]


class TestDamagedVault:
    def test_one_corrupt_project_does_not_hide_the_others(self, vault):
        create("good")
        create("broken")
        (vault / "broken" / "project.json").write_text('{"format": "tara-project", "revis')

        assert [p["id"] for p in listed()] == ["good"]
        res = client.get("/api/projects/broken")
        assert res.status_code == 500
        assert error_code(res) == "project_unreadable"

    def test_a_project_from_a_newer_studio_is_not_opened_or_overwritten(self, vault):
        create()
        path = vault / "diwali" / "project.json"
        meta = json.loads(path.read_text())
        path.write_text(json.dumps({**meta, "format_version": 99, "future_field": True}))

        assert listed() == []
        assert client.get("/api/projects/diwali").status_code == 500
        assert save("diwali", 1).status_code == 500
        assert json.loads(path.read_text())["future_field"] is True

    def test_stray_files_in_the_vault_are_ignored(self, vault):
        create()
        (vault / "notes.txt").write_text("not a project")
        (vault / "empty-dir").mkdir()
        assert [p["id"] for p in listed()] == ["diwali"]

    def test_no_temp_files_are_left_behind(self, vault):
        create(show=generate_circle(2, 30.0, 2.0))
        save("diwali", 1, show=generate_circle(2, 30.0, 2.0))
        assert sorted(p.name for p in (vault / "diwali").iterdir()) == ["project.json", "show.json"]

    def test_storage_that_cannot_be_written_is_a_503_naming_the_vault(self, vault):
        vault.write_text("a file where the vault directory should be")
        res = client.post("/api/projects", json={"name": "x", "document": document()})
        assert res.status_code == 503
        assert error_code(res) == "vault_unavailable"


class TestVaultLocation:
    def test_the_environment_wins_over_tara_toml(self, tmp_path):
        toml = tmp_path / "tara.toml"
        toml.write_text('[studio]\nvault_dir = "/from/toml"\n')
        assert vault_dir(toml, {project_store.VAULT_DIR_ENV: "/from/env"}) == Path("/from/env")
        assert vault_dir(toml, {}) == Path("/from/toml")

    def test_the_default_is_per_user_and_outside_the_checkout(self, tmp_path):
        chosen = vault_dir(tmp_path / "no-tara.toml", {})
        assert chosen == Path(project_store.DEFAULT_VAULT_DIR).expanduser()
        assert project_store.REPO_ROOT not in chosen.parents

    def test_a_home_relative_path_is_expanded(self, tmp_path):
        assert vault_dir(tmp_path / "no-tara.toml", {project_store.VAULT_DIR_ENV: "~/shows"}) == Path.home() / "shows"
