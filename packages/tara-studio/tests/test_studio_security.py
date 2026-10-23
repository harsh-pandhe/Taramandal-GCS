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
