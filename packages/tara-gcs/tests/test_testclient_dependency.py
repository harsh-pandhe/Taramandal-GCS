"""`fastapi.testclient` must import on a clean `pip install -r requirements.txt`.

starlette >=1.0 switched TestClient's HTTP backend from httpx to httpx2 and
kept it behind an optional extra, so an unpinned `fastapi` resolves a starlette
whose testclient raises at *import* time:

    RuntimeError: The starlette.testclient module requires the httpx2 package
    to be installed.

That is a collection error, not a test failure — it takes down every test in
any file that imports TestClient, before a single assertion runs. requirements.txt
lists httpx2 explicitly to prevent that; this test fails if it is ever dropped.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_testclient_imports_and_serves_a_request():
    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/ping")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
