"""Shared fixtures for the GCS tests.

Issue #64 put every API route behind operator authentication. Tests whose
subject is something else (lifecycle, pre-arm gates, uploads, aborts...) run
as an already-logged-in FLIGHT_COMMANDER, so they keep testing what they were
written for. Authentication itself is tested for real, with no override, in
test_auth_api.py (marked ``real_auth``).
"""

import pytest

from tara_gcs.auth import Operator, Role

COMMANDER = Operator("test-commander", Role.FLIGHT_COMMANDER)


def pytest_configure(config):
    config.addinivalue_line("markers", "real_auth: run without the logged-in test operator override")


@pytest.fixture(autouse=True)
def _logged_in_commander(request):
    if request.node.get_closest_marker("real_auth"):
        yield
        return
    from fastapi import Request

    from tara_gcs import main as gcs_main

    def as_commander(req: Request) -> Operator:
        req.state.operator = COMMANDER
        return COMMANDER

    overrides = gcs_main.app.dependency_overrides
    overrides[gcs_main.current_operator] = as_commander
    overrides[gcs_main.ws_operator] = lambda: COMMANDER
    yield
    overrides.pop(gcs_main.current_operator, None)
    overrides.pop(gcs_main.ws_operator, None)
