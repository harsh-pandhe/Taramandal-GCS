"""Security tests for Studio API and Project Vault (issue #130).

Verifies:
- CORS origin enforcement and rejection of unauthorized origins.
- Path traversal rejection in Project Vault.
- Mesh loading cannot access arbitrary server filesystem paths.
- Request size bounding on geometry inputs.
- Private key secrecy (never served over /api/signing).
"""

import tempfile
import pytest
from fastapi.testclient import TestClient

from tara_studio import project_store
from tara_studio.server import app


@pytest.fixture
def client():
    return TestClient(app)


class TestCorsOriginPolicy:
    """CORS origins are restricted by default and reject arbitrary untrusted origins."""

    def test_allowed_local_origin_receives_cors_headers(self, client):
        res = client.options(
            "/api/projects",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"
        assert res.headers.get("access-control-allow-credentials") == "true"

    def test_untrusted_origin_does_not_receive_cors_allow_origin(self, client):
        res = client.options(
            "/api/projects",
            headers={
                "Origin": "https://malicious-website.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert res.headers.get("access-control-allow-origin") != "https://malicious-website.com"


class TestProjectVaultSecurity:
    """Project Vault rejects path traversal, malicious characters, and maintains directory isolation."""

    def test_traversal_in_project_id_is_rejected_on_create(self, client):
        res = client.post(
            "/api/projects",
            json={
                "id": "../malicious",
                "name": "Traversal Attempt",
                "document": {"scenes": [], "dronesList": []},
            },
        )
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "invalid_project"

    def test_slash_in_project_id_is_rejected(self, client):
        res = client.post(
            "/api/projects",
            json={
                "id": "subdir/project",
                "name": "Subdir Attempt",
                "document": {"scenes": [], "dronesList": []},
            },
        )
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "invalid_project"

    def test_project_id_cannot_escape_vault_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = project_store.ProjectStore(tmp)
            with pytest.raises(project_store.InvalidProject):
                store.get("../../etc/passwd")

            with pytest.raises(project_store.InvalidProject):
                store.save("../../etc/passwd", base_revision=1, name="x", document={"scenes": [], "dronesList": []})

            with pytest.raises(project_store.InvalidProject):
                store.delete("..")


class TestVoxelizerInputSecurity:
    """Voxelizer input handling rejects server filesystem paths and bounds payload sizes."""

    def test_direct_file_path_mesh_loading_is_prohibited(self, client):
        res = client.post(
            "/api/voxelize",
            json={
                "source_type": "mesh",
                "content": "/etc/passwd",
                "drone_count": 10,
            },
        )
        assert res.status_code == 400
        assert "direct server file paths are not permitted" in res.json()["detail"]

    def test_unbounded_content_length_is_rejected_by_schema(self, client):
        oversized = "M 0 0 " * 150_000  # > 500,000 characters
        res = client.post(
            "/api/voxelize",
            json={
                "source_type": "svg",
                "content": oversized,
                "drone_count": 10,
            },
        )
        assert res.status_code == 422  # Pydantic validation error for max_length exceeded


class TestSigningKeySecrecy:
    """Signing endpoint only serves public metadata, never the private key."""

    def test_signing_status_exposes_no_private_key_material(self, client):
        res = client.get("/api/signing")
        assert res.status_code == 200
        data = res.json()
        assert "private" not in str(data).lower()
        if data.get("ready"):
            assert "key_id" in data
            assert "public_key" in data


class TestStudioOriginMatchesConfiguredPort:
    """Issue #155: the default CORS origins must include the Studio web UI's own origin."""

    @staticmethod
    def _preflight(client, origin):
        return client.options(
            "/api/validate",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )

    def test_configured_studio_web_origin_is_allowed(self, client):
        from tara_cli.config import load_config

        port = load_config().studio_web_port  # what `tara up` really serves the UI on
        for host in ("localhost", "127.0.0.1"):
            origin = f"http://{host}:{port}"
            res = self._preflight(client, origin)
            assert res.status_code == 200, origin
            assert res.headers.get("access-control-allow-origin") == origin

    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.example",
            "http://localhost:5174.evil.example",  # prefix look-alike
            "https://localhost:5174",  # wrong scheme
            "http://localhost:5199",  # another local port nobody configured
            "null",
        ],
    )
    def test_other_origins_stay_refused(self, client, origin):
        res = self._preflight(client, origin)
        assert res.headers.get("access-control-allow-origin") is None

    def test_origin_follows_tara_toml_port(self, tmp_path):
        from tara_studio.server import default_studio_origins, studio_web_port

        toml = tmp_path / "tara.toml"
        toml.write_text("[ports]\nstudio_web = 5200\n")
        assert studio_web_port(toml) == 5200
        origins = default_studio_origins(toml)
        assert "http://localhost:5200" in origins and "http://127.0.0.1:5200" in origins
        assert "*" not in origins

    @pytest.mark.parametrize("body", ["", "[ports]\nstudio_web = \"x\"\n", "[ports\n", "[ports]\nstudio_web = 0\n"])
    def test_missing_or_bad_config_falls_back_to_shipped_default(self, tmp_path, body):
        from tara_studio.server import studio_web_port

        toml = tmp_path / "tara.toml"
        toml.write_text(body)
        assert studio_web_port(toml) == 5174
        assert studio_web_port(tmp_path / "absent.toml") == 5174

    def test_wildcard_env_still_disables_credentials(self, monkeypatch):
        import importlib

        from tara_studio import server

        monkeypatch.setenv("STUDIO_ALLOWED_ORIGINS", "*")
        try:
            reloaded = importlib.reload(server)
            assert reloaded.allow_creds is False
        finally:
            monkeypatch.delenv("STUDIO_ALLOWED_ORIGINS")
            importlib.reload(server)

    def test_url_encoded_traversal_ids_fail_closed(self, client):
        # The threat model cites these payloads; they now have a test of their own.
        for pid in ("..%2F..", "..%2F..%2Fetc%2Fpasswd", "%2e%2e%2f"):
            res = client.get(f"/api/projects/{pid}")
            assert res.status_code in (400, 404, 422), (pid, res.status_code)

