"""Issues #94 / #89: the append-only audit log of AI proposals and decisions.

Driven through the HTTP API against a vault in a temp directory, and checked on
disk where the claim is about the file (append-only, hash chain, never touched by
a project save).
"""

import hashlib
import inspect
import json
import random
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tara_studio import audit_log, project_store, server
from tara_studio.audit_log import AUDIT_FILE, AuditLog
from tara_studio.generator import generate_circle
from tara_studio.project_store import InvalidProject, ProjectNotFound, ProjectStore
from tara_studio.server import app

client = TestClient(app)

EXPORT_GATE_JS = (
    Path(__file__).resolve().parents[2] / "tara-studio-web" / "src" / "engine" / "exportGate.js"
)


@pytest.fixture(autouse=True)
def vault(tmp_path, monkeypatch):
    """A fresh vault per test; never the developer's own projects or tara.toml."""
    root = tmp_path / "vault"
    monkeypatch.setenv(project_store.VAULT_DIR_ENV, str(root))
    monkeypatch.setattr(project_store, "DEFAULT_TOML", tmp_path / "tara.toml")
    return root


def make_project(project_id="diwali", **body) -> dict:
    payload = {
        "id": project_id, "name": "Diwali",
        "document": {"scenes": [{"id": "s1", "name": "S"}], "dronesList": [{"id": 0}], "rate": 4},
        **body,
    }
    res = client.post("/api/projects", json=payload)
    assert res.status_code == 201, res.text
    return res.json()["project"]


def entry(**over) -> dict:
    body = {
        "v": 1, "requestId": "req-1", "source": "typed", "request": "set hold of scene 1 to 8 s",
        "summary": "Scene 1 hold 6 s to 8 s",
        "ops": [{"type": "patch", "sceneIndex": 1, "fields": ["holdDurationSeconds"]}],
        "check": {"status": "PASSED", "kind": "open", "summary": "Passed all safety checks."},
        "decision": "applied", "reason": None,
    }
    body.update(over)
    return body


def post(body, project_id="diwali"):
    # Sent as text: json.dumps escapes a lone surrogate, which httpx's own encoder would refuse.
    return client.post(
        f"/api/projects/{project_id}/audit", content=json.dumps(body), headers={"Content-Type": "application/json"}
    )


def fetch(project_id="diwali", **params):
    res = client.get(f"/api/projects/{project_id}/audit", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def log_path(vault, project_id="diwali") -> Path:
    return vault / project_id / AUDIT_FILE


def error_code(res) -> str:
    return res.json()["detail"]["code"]


@pytest.fixture
def project():
    return make_project()


# ----------------------------------------------------------------- round trip

class TestAppendAndRead:
    def test_an_empty_log_reads_as_empty_and_creates_no_file(self, project, vault):
        got = fetch()
        assert got == {"entries": [], "total": 0, "full": False, "chainOk": True, "torn": False, "corruptLines": 0}
        assert not log_path(vault).exists()

    def test_an_entry_round_trips_with_every_client_field(self, project):
        sent = entry()
        res = post(sent)
        assert res.status_code == 201, res.text
        (got,) = fetch()["entries"]
        for key, value in sent.items():
            assert got[key] == value, key
        assert res.json()["entry"] == got

    def test_the_server_assigns_seq_at_project_id_and_the_chain_fields(self, project):
        post(entry(requestId="a"))
        post(entry(requestId="b"))
        newest, oldest = fetch()["entries"]
        assert (oldest["seq"], newest["seq"]) == (1, 2)
        assert oldest["projectId"] == newest["projectId"] == "diwali"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", oldest["at"])
        assert oldest["prev"] == ""
        assert newest["prev"] == oldest["hash"]
        assert re.fullmatch(r"[0-9a-f]{64}", oldest["hash"])

    def test_the_hash_is_sha256_of_the_canonical_entry_without_its_hash(self, project):
        post(entry())
        (got,) = fetch()["entries"]
        body = {k: v for k, v in got.items() if k != "hash"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        assert got["hash"] == hashlib.sha256(canonical.encode()).hexdigest()

    def test_newest_first(self, project):
        for n in range(5):
            post(entry(requestId=f"r{n}"))
        assert [e["requestId"] for e in fetch()["entries"]] == ["r4", "r3", "r2", "r1", "r0"]

    def test_the_file_is_one_ascii_line_per_entry_with_lf_endings(self, project, vault):
        post(entry(request='quote " newline \n tab \t unicode \u00e9 \u4e2d', requestId="x"))
        post(entry(requestId="y"))
        blob = log_path(vault).read_bytes()
        assert b"\r" not in blob
        assert blob.endswith(b"\n")
        assert blob.isascii()
        assert len(blob.split(b"\n")) == 3  # two lines and the empty rest
        assert fetch()["entries"][1]["request"] == 'quote " newline \n tab \t unicode \u00e9 \u4e2d'

    def test_every_append_is_flushed_to_disk(self, project, monkeypatch):
        synced = []
        monkeypatch.setattr(audit_log.os, "fsync", lambda fd: synced.append(fd))
        post(entry())
        assert len(synced) == 1

    def test_logs_of_two_projects_are_separate(self):
        make_project("a")
        make_project("b")
        post(entry(requestId="in-a"), "a")
        assert fetch("b")["total"] == 0
        assert fetch("a")["entries"][0]["projectId"] == "a"


class TestLimit:
    @pytest.fixture(autouse=True)
    def many(self, project):
        for n in range(120):
            post(entry(requestId=f"r{n}"))

    def test_the_default_is_the_latest_100(self):
        got = fetch()
        assert len(got["entries"]) == 100
        assert got["total"] == 120
        assert got["entries"][0]["seq"] == 120

    def test_a_limit_returns_that_many_newest_first(self):
        assert [e["seq"] for e in fetch(limit=3)["entries"]] == [120, 119, 118]

    def test_the_maximum_is_500(self):
        assert fetch(limit=500)["total"] == 120
        assert client.get("/api/projects/diwali/audit", params={"limit": 501}).status_code == 422

    @pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.5", ""])
    def test_a_bad_limit_is_a_422(self, bad):
        assert client.get("/api/projects/diwali/audit", params={"limit": bad}).status_code == 422


# ------------------------------------------------------------- the entry schema

class TestClientEntryIsStrict:
    @pytest.mark.parametrize("field,value", [
        ("seq", 7), ("at", "2020-01-01T00:00:00Z"), ("hash", "0" * 64), ("projectId", "other"), ("prev", ""),
        ("extra", 1),
    ])
    def test_a_field_the_server_owns_is_a_422(self, project, vault, field, value):
        assert post(entry(**{field: value})).status_code == 422
        assert not log_path(vault).exists()

    @pytest.mark.parametrize("path,value", [
        (("ops", 0, "extra"), 1),
        (("check", "extra"), 1),
    ])
    def test_nested_unknown_fields_are_a_422(self, project, path, value):
        body = entry()
        target = body
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
        assert post(body).status_code == 422

    @pytest.mark.parametrize("field,value", [
        ("source", "voice"), ("decision", "maybe"), ("decision", "log_full"), ("v", 2), ("v", "1"),
    ])
    def test_an_unknown_enum_value_is_a_422(self, project, field, value):
        assert post(entry(**{field: value})).status_code == 422

    def test_an_unknown_op_type_is_a_422(self, project):
        assert post(entry(ops=[{"type": "explode"}])).status_code == 422

    def test_an_unknown_check_status_or_kind_is_a_422(self, project):
        assert post(entry(check={"status": "OK", "kind": "open", "summary": ""})).status_code == 422
        assert post(entry(check={"status": "PASSED", "kind": "certified", "summary": ""})).status_code == 422

    @pytest.mark.parametrize("missing", ["v", "requestId", "source", "request", "summary", "decision"])
    def test_a_missing_required_field_is_a_422(self, project, missing):
        body = entry()
        del body[missing]
        assert post(body).status_code == 422

    @pytest.mark.parametrize("scene_index", [0, -1, 1.5, "1", True])
    def test_a_bad_scene_index_is_a_422(self, project, scene_index):
        assert post(entry(ops=[{"type": "patch", "sceneIndex": scene_index}])).status_code == 422

    def test_optional_parts_may_be_null_or_absent(self, project):
        body = entry(check=None, reason=None, ops=[])
        assert post(body).status_code == 201
        del body["check"], body["reason"], body["ops"]
        assert post(body).status_code == 201
        assert post(entry(check={"status": "REFUSED", "summary": "no kind"})).status_code == 201

    def test_check_kinds_are_the_ones_exportGate_js_produces(self):
        # engine/exportGate.js assigns `kind = '...'`; audit_log.CHECK_KINDS mirrors it.
        source = EXPORT_GATE_JS.read_text(encoding="utf-8")
        kinds = set(re.findall(r"\bkind = '(\w+)'", source))
        assert kinds == {"none", "open", "failed", "unverified"}
        assert set(audit_log.CHECK_KINDS) == kinds
        # What the model accepts, read from its JSON schema (public API), not pydantic internals.
        schema = audit_log.AuditCheck.model_json_schema()["properties"]["kind"]
        allowed = {v for branch in schema.get("anyOf", [schema]) for v in branch.get("enum", [])}
        assert allowed == kinds


class TestLengths:
    @pytest.mark.parametrize("make,limit", [
        (lambda n: entry(request="a" * n), 200),
        (lambda n: entry(summary="a" * n), 200),
        (lambda n: entry(reason="a" * n), 300),
        (lambda n: entry(check={"status": "PASSED", "kind": "open", "summary": "a" * n}), 200),
        (lambda n: entry(ops=[{"type": "patch", "fields": ["a" * n]}]), 40),
    ])
    def test_a_string_over_its_limit_is_refused_and_one_at_it_is_kept_whole(self, project, make, limit):
        assert post(make(limit + 1)).status_code == 422
        assert post(make(limit)).status_code == 201

    def test_the_request_id_is_1_to_64_safe_characters(self, project):
        assert post(entry(requestId="a" * 64)).status_code == 201
        for bad in ("", "a" * 65, "has space", "a.b", "a/b", "é", "abc\n", "a\x00b"):
            assert post(entry(requestId=bad)).status_code == 422, repr(bad)

    def test_list_lengths(self, project):
        assert post(entry(ops=[{"type": "other"}] * 20)).status_code == 201
        assert post(entry(ops=[{"type": "other"}] * 21)).status_code == 422
        assert post(entry(ops=[{"type": "other", "fields": ["f"] * 10}])).status_code == 201
        assert post(entry(ops=[{"type": "other", "fields": ["f"] * 11}])).status_code == 422

    def test_nothing_is_truncated_on_the_way_in(self, project):
        post(entry(request="x" * 200, reason="y" * 300))
        (got,) = fetch()["entries"]
        assert got["request"] == "x" * 200 and got["reason"] == "y" * 300

    def test_a_serialized_entry_must_stay_under_4kb(self, project):
        big = [{"type": "patch", "fields": ["f" * 40] * 10}] * 20  # each string is valid; the whole is not
        res = post(entry(ops=big))
        assert res.status_code == 422
        assert "4096" in res.text
        small = [{"type": "patch", "fields": ["f" * 40] * 10}] * 5
        assert post(entry(ops=small)).status_code == 201

    def test_the_4kb_measure_is_the_stored_form_so_escapes_count(self, project):
        # 200 CJK characters are 1200 bytes once escaped for the ASCII file.
        res = post(entry(request="\u4e2d" * 200, summary="\u4e2d" * 200, reason="\u4e2d" * 300))
        assert res.status_code == 422


class TestControlCharacters:
    BAD = ["\x00", "a\x00b", "\x07", "\x1b[31m", "\x7f", "\x85", "\r", "\ud800"]

    @pytest.mark.parametrize("bad", BAD)
    @pytest.mark.parametrize("where", ["request", "summary", "reason", "check", "field"])
    def test_control_characters_are_a_422_everywhere(self, project, bad, where):
        body = entry()
        if where == "check":
            body["check"]["summary"] = bad
        elif where == "field":
            body["ops"][0]["fields"] = [bad]
        else:
            body[where] = bad
        assert post(body).status_code == 422

    def test_newline_and_tab_are_allowed_and_cannot_break_the_line_format(self, project, vault):
        post(entry(request="line1\nline2\ttabbed", reason='{"seq": 99}\n{"fake": true}'))
        assert len(log_path(vault).read_bytes().splitlines()) == 1
        (got,) = fetch()["entries"]
        assert got["request"] == "line1\nline2\ttabbed"
        assert fetch()["chainOk"] is True


# ------------------------------------------------------------------ the chain

def lines_of(vault) -> list[bytes]:
    return log_path(vault).read_bytes().split(b"\n")[:-1]


def write_lines(vault, lines: list[bytes]) -> None:
    log_path(vault).write_bytes(b"\n".join(lines) + b"\n")


@pytest.fixture
def five(project):
    for n in range(5):
        post(entry(requestId=f"r{n}", summary=f"summary {n}"))


class TestChain:
    def test_a_fresh_log_verifies(self, five):
        assert fetch()["chainOk"] is True

    def test_editing_one_byte_breaks_it(self, five, vault):
        lines = lines_of(vault)
        lines[2] = lines[2].replace(b"summary 2", b"summary 9")
        write_lines(vault, lines)
        assert fetch()["chainOk"] is False

    def test_editing_a_hash_to_match_still_breaks_the_next_link(self, five, vault):
        lines = lines_of(vault)
        entry2 = json.loads(lines[1])
        entry2["summary"] = "forged"
        entry2["hash"] = audit_log._entry_hash(entry2)
        lines[1] = audit_log._canonical(entry2)
        write_lines(vault, lines)
        assert fetch()["chainOk"] is False

    def test_deleting_a_line_breaks_it(self, five, vault):
        lines = lines_of(vault)
        del lines[2]
        write_lines(vault, lines)
        assert fetch()["chainOk"] is False

    def test_swapping_two_lines_breaks_it(self, five, vault):
        lines = lines_of(vault)
        lines[1], lines[3] = lines[3], lines[1]
        write_lines(vault, lines)
        assert fetch()["chainOk"] is False

    def test_deleting_the_first_line_breaks_it(self, five, vault):
        write_lines(vault, lines_of(vault)[1:])
        assert fetch()["chainOk"] is False

    def test_a_line_copied_in_from_another_project_breaks_it(self, five, vault):
        make_project("other")
        post(entry(), "other")
        foreign = lines_of(vault)[0]
        other_lines = (vault / "other" / AUDIT_FILE).read_bytes().split(b"\n")[:-1]
        lines = lines_of(vault)
        lines[0] = other_lines[0]
        write_lines(vault, lines)
        assert fetch()["chainOk"] is False
        assert foreign != other_lines[0]

    def test_a_line_that_is_not_json_breaks_it_and_is_counted(self, five, vault):
        lines = lines_of(vault)
        lines[2] = b"garbage"
        write_lines(vault, lines)
        got = fetch()
        assert got["chainOk"] is False
        assert got["corruptLines"] == 1

    def test_nan_in_a_hand_edited_file_does_not_crash_the_reader(self, five, vault):
        lines = lines_of(vault)
        lines[1] = lines[1].replace(b'"seq":2', b'"seq":2,"x":NaN')
        write_lines(vault, lines)
        assert fetch()["chainOk"] is False

    def test_truncating_mid_line_is_a_torn_tail_and_breaks_it(self, five, vault):
        blob = log_path(vault).read_bytes()
        log_path(vault).write_bytes(blob[:-40])
        got = fetch()
        assert got["chainOk"] is False
        assert got["torn"] is True

    def test_removing_whole_lines_from_the_end_is_not_detectable(self, five, vault):
        """The documented limit of a hash chain with no outside anchor."""
        write_lines(vault, lines_of(vault)[:3])
        got = fetch()
        assert got["total"] == 3
        assert got["chainOk"] is True

    def test_reading_never_modifies_the_file(self, five, vault):
        before = log_path(vault).read_bytes()
        fetch()
        fetch(limit=2)
        assert log_path(vault).read_bytes() == before


class TestTornTail:
    FRAGMENT = b'{"v":1,"requestId":"cra'

    def tear(self, vault):
        with open(log_path(vault), "ab") as f:
            f.write(self.FRAGMENT)

    def test_a_torn_tail_is_reported_ignored_and_the_intact_entries_still_read(self, five, vault):
        self.tear(vault)
        got = fetch()
        assert got["torn"] is True
        assert got["chainOk"] is False
        assert got["total"] == 5
        assert [e["requestId"] for e in got["entries"]] == ["r4", "r3", "r2", "r1", "r0"]

    def test_the_next_append_starts_a_fresh_line_and_chains_from_the_last_complete_entry(self, five, vault):
        last = fetch()["entries"][0]
        self.tear(vault)
        res = post(entry(requestId="after"))
        assert res.status_code == 201
        stored = res.json()["entry"]
        assert stored["seq"] == 6
        assert stored["prev"] == last["hash"]

        blob = log_path(vault).read_bytes()
        assert self.FRAGMENT + b"\n" in blob          # the evidence is kept, not deleted
        assert blob.endswith(b"\n")
        assert blob.count(b"\n") == 7                 # 5 entries + ended fragment + the new entry

    def test_after_a_repair_the_log_reads_normally_but_stays_flagged(self, five, vault):
        self.tear(vault)
        post(entry(requestId="after"))
        got = fetch()
        assert got["torn"] is False
        assert got["corruptLines"] == 1
        assert got["chainOk"] is False                # damage is never healed silently
        assert got["total"] == 6
        assert [e["seq"] for e in got["entries"]] == [6, 5, 4, 3, 2, 1]
        post(entry(requestId="more"))
        assert fetch()["total"] == 7

    def test_a_torn_first_line_is_handled(self, project, vault):
        log_path(vault).write_bytes(self.FRAGMENT)
        assert fetch()["total"] == 0
        stored = post(entry()).json()["entry"]
        assert (stored["seq"], stored["prev"]) == (1, "")


# ------------------------------------------------------------------------ caps

class TestCap:
    def test_the_count_cap_writes_one_marker_then_refuses(self, project, vault, monkeypatch):
        monkeypatch.setattr(audit_log, "MAX_ENTRIES", 5)
        for n in range(4):
            assert post(entry(requestId=f"r{n}")).status_code == 201
        assert fetch()["full"] is False

        res = post(entry(requestId="over"))
        assert res.status_code == 409
        assert error_code(res) == "audit_full"
        got = fetch()
        assert got["full"] is True
        assert got["total"] == 5
        marker = got["entries"][0]
        assert marker["decision"] == "log_full"
        assert marker["source"] == "typed"
        assert marker["request"] == ""
        assert marker["summary"] == audit_log.FULL_SUMMARY
        assert got["chainOk"] is True

        frozen = log_path(vault).read_bytes()
        for _ in range(3):
            assert post(entry()).status_code == 409
        assert log_path(vault).read_bytes() == frozen
        assert [e["decision"] for e in fetch(limit=500)["entries"]].count("log_full") == 1

    def test_the_byte_cap_writes_one_marker_then_refuses(self, project, vault, monkeypatch):
        monkeypatch.setattr(audit_log, "MAX_BYTES", 3000)
        statuses = [post(entry(requestId=f"r{n}")).status_code for n in range(20)]
        assert statuses[0] == 201
        assert 409 in statuses
        first_refusal = statuses.index(409)
        assert set(statuses[first_refusal:]) == {409}
        size = log_path(vault).stat().st_size
        assert size <= 3000
        got = fetch(limit=500)
        assert got["full"] is True
        assert got["chainOk"] is True
        assert got["entries"][0]["decision"] == "log_full"
        assert [e["decision"] for e in got["entries"]].count("log_full") == 1
        assert got["total"] == first_refusal + 1          # the entries kept, plus the marker

    def test_nothing_is_dropped_at_the_cap(self, project, monkeypatch):
        monkeypatch.setattr(audit_log, "MAX_ENTRIES", 4)
        for n in range(10):
            post(entry(requestId=f"r{n}"))
        ids = [e["requestId"] for e in fetch()["entries"]]
        assert ids == ["log_full", "r2", "r1", "r0"]

    def test_the_real_limits_are_5000_entries_and_1_mb(self):
        assert audit_log.MAX_ENTRIES == 5000
        assert audit_log.MAX_BYTES == 1_000_000

    def test_a_full_log_stays_full_for_other_projects_independently(self, project, monkeypatch):
        make_project("other")
        monkeypatch.setattr(audit_log, "MAX_ENTRIES", 2)
        post(entry())
        assert post(entry()).status_code == 409
        assert post(entry(), "other").status_code == 201


# --------------------------------------------------------------- no way to edit

class TestNoEditOrDelete:
    @pytest.mark.parametrize("method", ["put", "patch", "delete"])
    def test_the_audit_route_has_no_write_verbs_other_than_post(self, five, vault, method):
        before = log_path(vault).read_bytes()
        res = getattr(client, method)("/api/projects/diwali/audit")
        assert res.status_code == 405
        res = client.request(method.upper(), "/api/projects/diwali/audit", json=entry())
        assert res.status_code == 405
        assert log_path(vault).read_bytes() == before

    @pytest.mark.parametrize("method", ["get", "put", "patch", "delete", "post"])
    def test_there_is_no_route_to_one_entry(self, five, vault, method):
        before = log_path(vault).read_bytes()
        for tail in ("/1", "/seq/1"):
            res = getattr(client, method)(f"/api/projects/diwali/audit{tail}")
            assert res.status_code in (404, 405)
        assert log_path(vault).read_bytes() == before

    def test_only_get_and_post_are_registered_for_the_audit_paths(self):
        """Read from the OpenAPI document, which is public API, not from app.routes: newer FastAPI
        no longer lists included routers' routes there (it holds an opaque wrapper per router), so
        a walk of app.routes finds nothing. The real-request tests above cover a route that is
        hidden from the schema."""
        http_verbs = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
        audit_paths = {
            path: {verb for verb in item if verb in http_verbs}
            for path, item in app.openapi()["paths"].items()
            if "/audit" in path
        }
        assert set(audit_paths) == {"/api/projects/{project_id}/audit"}   # found something, and only the one path
        found = set().union(*audit_paths.values())
        assert found, "found no audit methods at all: the check would pass vacuously"
        assert found == {"get", "post"}

    @pytest.mark.parametrize("method", ["put", "patch", "delete", "options", "trace"])
    def test_every_other_verb_on_the_audit_path_is_refused_with_405(self, five, vault, method):
        before = log_path(vault).read_bytes()
        assert client.request(method.upper(), "/api/projects/diwali/audit").status_code == 405
        assert log_path(vault).read_bytes() == before

    def test_get_and_post_work_on_the_audit_path(self, project):
        assert client.get("/api/projects/diwali/audit").status_code == 200
        assert post(entry()).status_code == 201

    def test_the_log_object_has_no_method_that_rewrites_or_deletes(self):
        public = {n for n in dir(AuditLog) if not n.startswith("_")}
        assert public == {"append", "read"}


# --------------------------------------------------- projects, ids and the vault

class TestProjectsAndIds:
    def test_an_unknown_project_is_a_404_and_creates_nothing(self, vault):
        res = post(entry(), "never-saved")
        assert res.status_code == 404
        assert error_code(res) == "project_not_found"
        assert client.get("/api/projects/never-saved/audit").status_code == 404
        assert not vault.exists()

    def test_a_trashed_project_is_a_404_and_no_file_is_created(self, project, vault):
        post(entry())
        assert client.delete("/api/projects/diwali").status_code == 204
        assert post(entry()).status_code == 404
        assert client.get("/api/projects/diwali/audit").status_code == 404
        assert not (vault / "diwali").exists()

    def test_the_log_moves_to_the_trash_with_its_project(self, project, vault):
        post(entry())
        before = log_path(vault).read_bytes()
        client.delete("/api/projects/diwali")
        (trashed,) = (vault / ".trash").iterdir()
        assert (trashed / AUDIT_FILE).read_bytes() == before

    def test_a_new_project_on_a_reused_id_starts_with_an_empty_log(self, project):
        post(entry())
        client.delete("/api/projects/diwali")
        make_project("diwali")
        assert fetch()["total"] == 0

    @pytest.mark.parametrize("bad_id", [".trash", "has.dot", "-leading-dash", "x" * 65, "sp ace", "caf\u00e9", "\uff21bc", "a\u202eb"])
    def test_an_id_that_is_not_a_safe_directory_name_is_a_422_like_the_other_routes(self, bad_id, vault):
        assert post(entry(), bad_id).status_code == 422
        assert client.get(f"/api/projects/{bad_id}/audit").status_code == 422
        assert not vault.exists()

    @pytest.mark.parametrize("bad_id", ["..", "../outside", "/etc", "C:\\x", "a/b", "", ".trash", "..\\x"])
    def test_no_id_reaches_outside_the_vault(self, bad_id, vault, tmp_path):
        """On the log directly, because an HTTP client normalises `..` away first."""
        (tmp_path / "outside").mkdir()
        log = AuditLog(ProjectStore(vault))
        body = audit_log.AuditEntryIn(**entry())
        for attempt in (lambda: log.read(bad_id), lambda: log.append(bad_id, body)):
            with pytest.raises((InvalidProject, ProjectNotFound)):
                attempt()
        assert list((tmp_path / "outside").iterdir()) == []
        assert not vault.exists()


# --------------------------------------------- never part of the project or show

class TestKeptApartFromTheProject:
    CANARY = "ZZ-AUDIT-CANARY-ZZ"

    def versions(self, doc):
        return [{"id": f"v-{n}", "savedAt": f"2026-10-06T10:0{n}:00.000Z", **doc} for n in range(3)]

    def test_saves_restores_and_renames_leave_the_log_byte_identical(self, vault):
        doc = {"scenes": [{"id": "s1"}], "dronesList": [{"id": 0}]}
        project = make_project("diwali", show=generate_circle(2, 30.0, 2.0), versions=self.versions(doc))
        for n in range(3):
            post(entry(requestId=f"r{n}", request=self.CANARY))
        before = log_path(vault).read_bytes()
        mtime = log_path(vault).stat().st_mtime_ns

        revision = project["revision"]
        for name in ("Diwali", "Renamed", "Renamed again"):
            res = client.put("/api/projects/diwali", json={
                "name": name, "document": doc, "base_revision": revision,
                "show": generate_circle(2, 30.0, 2.0), "versions": self.versions(doc),
            })
            assert res.status_code == 200, res.text
            revision = res.json()["project"]["revision"]
        # "Restore" is a save of an older version's content.
        res = client.put("/api/projects/diwali", json={
            "name": "Restored", "document": {**doc, "rate": 9}, "base_revision": revision,
            "versions": self.versions(doc)[:1],
        })
        assert res.status_code == 200

        assert log_path(vault).read_bytes() == before
        assert log_path(vault).stat().st_mtime_ns == mtime
        assert fetch()["chainOk"] is True

    def test_nothing_the_project_stores_mentions_the_log(self, vault):
        doc = {"scenes": [{"id": "s1"}], "dronesList": [{"id": 0}]}
        make_project("diwali", show=generate_circle(2, 30.0, 2.0))
        post(entry(request=self.CANARY, summary=self.CANARY, reason=self.CANARY))
        client.put("/api/projects/diwali", json={
            "name": "Diwali", "document": doc, "base_revision": 1, "show": generate_circle(2, 30.0, 2.0),
            "versions": self.versions(doc),
        })
        for name in ("project.json", "show.json"):
            assert self.CANARY.encode() not in (vault / "diwali" / name).read_bytes()
        meta = json.loads((vault / "diwali" / "project.json").read_text())
        assert not any("audit" in key.lower() for key in meta)
        assert not any("audit" in key.lower() for key in meta["document"])
        assert self.CANARY not in client.get("/api/projects/diwali").text
        assert self.CANARY not in client.get("/api/projects").text

    def test_the_export_and_validate_endpoints_never_see_the_log(self, vault):
        make_project("diwali")
        post(entry(request=self.CANARY))
        choreo = {"drones": [{"id": 0, "waypoints": [
            {"time": 0.0, "x": 0.0, "y": 0.0, "z": -120.0, "yaw": 0.0},
            {"time": 5.0, "x": 0.0, "y": 0.0, "z": -120.0, "yaw": 0.0},
        ]}]}
        res = client.post("/api/export", json={"choreo": choreo, "format": "json"})
        assert res.status_code == 200
        assert self.CANARY not in res.text
        assert "audit" not in res.text.lower()
        res = client.post("/api/validate", json=choreo)
        assert self.CANARY not in res.text

    def test_the_project_store_does_not_know_the_log_exists(self):
        """So a save, restore or rename cannot touch it; the file is only moved with its directory."""
        assert "audit" not in inspect.getsource(project_store).lower()

    def test_the_server_only_mounts_the_audit_router(self):
        hits = [ln.strip() for ln in inspect.getsource(server).splitlines() if "audit" in ln.lower()]
        assert hits == [
            "from tara_studio.audit_api import router as audit_router",
            "app.include_router(audit_router)",
        ]


# ------------------------------------------------------------------ concurrency

class TestConcurrency:
    def test_parallel_appends_have_contiguous_seq_and_a_valid_chain(self, project):
        threads, per_thread = 8, 12
        start = threading.Barrier(threads)

        def work(t):
            start.wait()
            return [post(entry(requestId=f"t{t}-{n}")).status_code for n in range(per_thread)]

        with ThreadPoolExecutor(threads) as pool:
            statuses = [s for batch in pool.map(work, range(threads)) for s in batch]
        assert set(statuses) == {201}
        got = fetch(limit=500)
        assert got["total"] == threads * per_thread
        assert sorted(e["seq"] for e in got["entries"]) == list(range(1, threads * per_thread + 1))
        assert got["chainOk"] is True
        # Within one thread the order it appended in is kept.
        for t in range(threads):
            mine = [int(e["requestId"].split("-")[1]) for e in reversed(got["entries"]) if e["requestId"].startswith(f"t{t}-")]
            assert mine == sorted(mine)

    def test_parallel_appends_through_separate_stores_are_serialised_too(self, project, vault):
        body = audit_log.AuditEntryIn(**entry())

        def work(_):
            return AuditLog(ProjectStore(vault)).append("diwali", body)["seq"]

        with ThreadPoolExecutor(6) as pool:
            seqs = list(pool.map(work, range(60)))
        assert sorted(seqs) == list(range(1, 61))
        assert fetch(limit=500)["chainOk"] is True


# ------------------------------------------------------------------------- fuzz

ALPHABET = (
    list("abcXYZ019 _-.,;:!?'\"\\/{}[]<>%$#@&*+=~|")
    + ["\n", "\t", "\u00e9", "\u4e2d", "\U0001f6a8", "\u202e", "\u200b", "\u2028"]
)


def rtext(rng: random.Random, limit: int) -> str:
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(0, limit)))


def random_entry(rng: random.Random, n: int) -> dict:
    ops = [
        {"type": rng.choice(["patch", "add", "move", "delete", "duplicate", "other"]),
         **({"sceneIndex": rng.randint(1, 50)} if rng.random() < 0.7 else {}),
         "fields": [rtext(rng, 40) for _ in range(rng.randint(0, 3))]}
        for _ in range(rng.randint(0, 4))
    ]
    check = None if rng.random() < 0.3 else {
        "status": rng.choice(["PASSED", "FAILED", "REFUSED", "UNVERIFIED"]),
        **({"kind": rng.choice(audit_log.CHECK_KINDS)} if rng.random() < 0.8 else {}),
        "summary": rtext(rng, 80),
    }
    return {
        "v": 1, "requestId": f"fz-{n}", "source": rng.choice(["typed", "planner"]),
        "request": rtext(rng, 80), "summary": rtext(rng, 80), "ops": ops, "check": check,
        "decision": rng.choice([
            "applied", "dismissed", "stale", "blocked", "superseded", "refused",
            "planner_unavailable", "planner_timeout", "planner_failed",
        ]),
        "reason": None if rng.random() < 0.4 else rtext(rng, 120),
    }


def test_fuzz_random_valid_entries_read_back_unchanged_in_reverse_with_a_valid_chain(project, vault):
    rng = random.Random(20261115)
    sent = [random_entry(rng, n) for n in range(150)]
    for body in sent:
        res = post(body)
        assert res.status_code == 201, (res.text, body)
    got = fetch(limit=500)
    assert got["chainOk"] is True and got["total"] == 150
    for stored, original in zip(got["entries"], reversed(sent), strict=True):
        expected = dict(original)
        expected["ops"] = [{"sceneIndex": None, **op} for op in original["ops"]]
        if original["check"] is not None:
            expected["check"] = {"kind": None, **original["check"]}
        for key, value in expected.items():
            assert stored[key] == value, key
    assert [e["seq"] for e in got["entries"]] == list(range(150, 0, -1))
    # And the file itself is exactly 150 ASCII lines.
    blob = log_path(vault).read_bytes()
    assert blob.isascii() and blob.count(b"\n") == 150


# ------------------------------------------------- review: races, errors, input

def fill(vault, count: int, project_id: str = "diwali") -> None:
    """A valid chain of `count` entries written straight to the file (appending that many one by one is quadratic)."""
    lines, prev = [], ""
    for seq in range(1, count + 1):
        e = {**entry(requestId=f"f{seq}"), "seq": seq, "at": "2026-11-15T00:00:00.000Z", "projectId": project_id, "prev": prev}
        e["hash"] = audit_log._entry_hash(e)
        prev = e["hash"]
        lines.append(audit_log._canonical(e).decode() + "\n")
    (vault / project_id / AUDIT_FILE).write_text("".join(lines), encoding="ascii", newline="")


def chain_ok_of(directory: Path) -> bool:
    return audit_log._scan((directory / AUDIT_FILE).read_bytes(), "diwali").chain_ok


class TestVanishingProject:
    """The lock keeps another request out between the existence check and the write; these
    inject the change from inside, to show the write itself never lands in the wrong place."""

    def inject(self, monkeypatch, vault, action):
        real = AuditLog._seal
        done = []

        def seal(self, *args, **kwargs):
            if not done:
                done.append(1)
                action(ProjectStore(vault))
            return real(self, *args, **kwargs)

        monkeypatch.setattr(AuditLog, "_seal", seal)

    def test_a_project_deleted_between_the_check_and_the_write_is_a_404_and_nothing_is_created(self, project, vault, monkeypatch):
        post(entry())
        self.inject(monkeypatch, vault, lambda store: store.delete("diwali"))
        res = post(entry(requestId="late"))
        assert res.status_code == 404
        assert error_code(res) == "project_not_found"
        assert not (vault / "diwali").exists()
        (trashed,) = (vault / ".trash").iterdir()
        assert len((trashed / AUDIT_FILE).read_bytes().splitlines()) == 1   # the old log did not get the entry

    def test_a_project_re_created_under_the_same_id_does_not_get_the_old_projects_entry(self, project, vault, monkeypatch):
        post(entry())

        def swap(store):
            store.delete("diwali")
            store.create(project_id="diwali", name="New", document={"scenes": [], "dronesList": []})

        self.inject(monkeypatch, vault, swap)
        assert post(entry(requestId="late")).status_code == 404
        assert not log_path(vault).exists()
        assert fetch()["total"] == 0
        (trashed,) = (vault / ".trash").iterdir()
        assert len((trashed / AUDIT_FILE).read_bytes().splitlines()) == 1

    def test_a_directory_that_vanished_without_the_store_is_a_404_not_a_created_folder(self, project, vault, monkeypatch):
        import shutil
        self.inject(monkeypatch, vault, lambda store: shutil.rmtree(vault / "diwali"))
        assert post(entry()).status_code == 404
        assert not (vault / "diwali").exists()

    def test_deleting_while_appending_in_other_threads_never_recreates_the_project(self, project, vault):
        stop = threading.Event()
        statuses = []

        def writer():
            while not stop.is_set():
                statuses.append(post(entry()).status_code)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        while len(statuses) < 10:
            pass
        assert client.delete("/api/projects/diwali").status_code == 204
        stop.set()
        for t in threads:
            t.join()
        assert set(statuses) <= {201, 404}
        assert statuses[-1] == 404
        assert not (vault / "diwali").exists()
        (trashed,) = (vault / ".trash").iterdir()
        assert chain_ok_of(trashed)


class TestCapBoundaryIsOneCriticalSection:
    def test_simultaneous_appends_at_the_cap_give_one_marker_and_no_duplicate_seq(self, project, vault, monkeypatch):
        monkeypatch.setattr(audit_log, "MAX_ENTRIES", 20)
        threads = 10
        start = threading.Barrier(threads)

        def work(t):
            start.wait()
            return [post(entry(requestId=f"t{t}-{n}")).status_code for n in range(6)]

        with ThreadPoolExecutor(threads) as pool:
            statuses = [s for batch in pool.map(work, range(threads)) for s in batch]
        assert statuses.count(201) == 19
        assert set(statuses) == {201, 409}
        got = fetch(limit=500)
        assert got["total"] == 20 and got["full"] is True and got["chainOk"] is True
        assert [e["seq"] for e in reversed(got["entries"])] == list(range(1, 21))
        assert [e["decision"] for e in got["entries"]].count("log_full") == 1

    def test_the_real_5000_entry_cap_writes_one_marker(self, project, vault):
        fill(vault, 4999)
        assert fetch()["full"] is False
        assert post(entry()).status_code == 409
        got = fetch(limit=500)
        assert got["total"] == 5000 and got["full"] is True and got["chainOk"] is True
        assert got["entries"][0]["decision"] == "log_full"
        assert post(entry()).status_code == 409
        assert fetch()["total"] == 5000

    def test_a_full_log_reads_in_well_under_a_second(self, project, vault):
        import time
        fill(vault, 5000)
        start = time.perf_counter()
        got = fetch(limit=500)
        elapsed = time.perf_counter() - start
        assert got["total"] == 5000 and got["chainOk"] is True
        assert elapsed < 1.0, elapsed


class TestStorageErrors:
    def break_log(self, vault):
        log_path(vault).mkdir()   # a directory where the log file should be: unreadable and unwritable

    def test_an_unwritable_log_is_a_503_that_does_not_name_a_path(self, project, vault):
        self.break_log(vault)
        res = post(entry())
        assert res.status_code == 503
        assert error_code(res) == "vault_unavailable"
        assert "Vault" in res.json()["detail"]["message"]
        assert str(vault) not in res.text and "audit.jsonl" not in res.text and "Errno" not in res.text

    def test_an_unreadable_log_is_a_503_too(self, project, vault):
        self.break_log(vault)
        res = client.get("/api/projects/diwali/audit")
        assert res.status_code == 503
        assert str(vault) not in res.text and "Traceback" not in res.text

    def test_a_full_disk_while_writing_is_a_503(self, project, vault, monkeypatch):
        def full(fd):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(audit_log.os, "fsync", full)
        res = post(entry())
        assert res.status_code == 503
        assert "No space" not in res.text

    def test_garbage_bytes_in_the_log_are_reported_not_a_500(self, project, vault):
        log_path(vault).write_bytes(b"\xff\xfe\x00 not utf-8\n" + b"[" * 5000 + b"\n")
        got = fetch()
        assert got["chainOk"] is False and got["corruptLines"] == 2 and got["total"] == 0
        assert post(entry()).status_code == 201


class TestHostileInput:
    def send(self, content, content_type="application/json", project_id="diwali"):
        headers = {"Content-Type": content_type} if content_type else {}
        return client.post(f"/api/projects/{project_id}/audit", content=content, headers=headers)

    def test_a_10_mb_body_is_refused_with_413_and_nothing_is_stored(self, project, vault):
        res = self.send(b'{"v":1,"request":"' + b"a" * (10 * 1024 * 1024) + b'"}')
        assert res.status_code == 413
        assert error_code(res) == "body_too_large"
        assert not log_path(vault).exists()

    def test_a_body_without_a_content_length_is_cut_off_while_it_streams(self, project, vault):
        def chunks():
            for _ in range(2000):            # 2000 x 8 KB = 16 MB if it were all read
                yield b"a" * 8192

        res = self.send(chunks())
        assert res.status_code == 413
        assert not log_path(vault).exists()

    def test_the_limit_is_64_kb_and_a_normal_entry_is_far_below_it(self, project):
        from tara_studio import audit_api
        assert audit_api.MAX_BODY_BYTES == 64 * 1024
        assert len(json.dumps(entry())) < 1000

    def test_ten_thousand_ops_are_refused(self, project):
        assert self.send(json.dumps(entry(ops=[{"type": "other"}] * 10000))).status_code == 413

    def test_three_thousand_ops_under_the_byte_limit_are_still_a_422(self, project):
        body = json.dumps(entry(ops=[{"type": "other"}] * 3000), separators=(",", ":"))
        assert len(body) < 64 * 1024
        assert self.send(body).status_code == 422

    def test_deeply_nested_json_is_a_clean_422(self, project):
        for depth in (100, 5000, 30000):
            res = self.send("[" * depth + "]" * depth)
            assert res.status_code == 422, depth
        assert self.send('{"v":1,"reason":' + "[" * 30000).status_code == 422

    def test_a_huge_string_in_an_unknown_field_is_refused(self, project, vault):
        assert self.send(json.dumps({**entry(), "x": "a" * 200_000})).status_code == 413
        assert self.send(json.dumps({**entry(), "x": "a" * 20_000})).status_code == 422
        assert not log_path(vault).exists()

    @pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "", None, "application/jsonx"])
    def test_a_wrong_content_type_is_a_415(self, project, content_type):
        assert self.send(json.dumps(entry()), content_type).status_code == 415

    def test_json_with_a_charset_parameter_is_accepted(self, project):
        assert self.send(json.dumps(entry()), "application/json; charset=utf-8").status_code == 201

    @pytest.mark.parametrize("content", [b"", b"not json", b"\xff\xfe", b"[]", b"null", b'"text"', b"123", b"{"])
    def test_a_body_that_is_not_an_entry_object_is_a_422(self, project, content):
        assert self.send(content).status_code == 422

    def test_validation_errors_do_not_echo_the_input_or_the_exception(self, project):
        res = self.send(json.dumps(entry(request="x" * 201)))
        assert res.status_code == 422
        assert "x" * 50 not in res.text
        assert "Traceback" not in res.text

    def test_the_project_is_checked_after_the_body(self, vault):
        assert self.send(json.dumps(entry()), project_id="nope").status_code == 404
        assert self.send(b"junk", project_id="nope").status_code == 422


class TestSecondLineOfDefence:
    def test_a_missing_directory_at_open_time_is_a_404_even_if_the_identity_check_were_fooled(self, project, vault, monkeypatch):
        import shutil

        real = AuditLog._seal

        def seal(self, *args, **kwargs):
            shutil.rmtree(vault / "diwali")
            return real(self, *args, **kwargs)

        monkeypatch.setattr(audit_log, "_identity", lambda directory: ("fooled",))
        monkeypatch.setattr(AuditLog, "_seal", seal)
        res = post(entry())
        assert res.status_code == 404
        assert not (vault / "diwali").exists()
