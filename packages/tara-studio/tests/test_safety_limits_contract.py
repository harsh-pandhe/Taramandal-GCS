"""Issue #97: the Studio UI judges safety metrics by the backend's live limits.

The Safety tab used to colour its metrics against hardcoded thresholds that had
drifted from tara_core.safety.DEFAULT_LIMITS. It now reads
``validation["limits"]``, which the server attaches to every report. This
module pins both ends of that contract:

* the limits the server sends are ``DEFAULT_LIMITS``, the exact object the
  validator judged the show against, and not a copy;
* ``tara-studio-web/src/engine/__fixtures__/validation-response.json``, which
  the frontend tests render ``SafetyPanel`` from, is byte-for-byte what the live
  ``/api/validate`` returns. Change the limits or rename a field on either
  side, and one of the two test suites fails.

To refresh the fixture after an intentional limits change:

    PYTHONPATH=packages/tara-core:packages/tara-studio \\
        python packages/tara-studio/tests/test_safety_limits_contract.py
"""

import json
from dataclasses import asdict
from pathlib import Path

from tara_core import DEFAULT_LIMITS
from tara_studio import server

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tara-studio-web" / "src" / "engine" / "__fixtures__" / "validation-response.json"
)


def _pair_show(separation_m: float, duration_s: float = 2.0) -> dict:
    """Two drones hovering ``separation_m`` apart for ``duration_s``."""
    return {"drones": [
        {"id": i, "waypoints": [
            {"time": t, "x": 0.0, "y": i * separation_m, "z": -5.0, "yaw": 0.0} for t in (0.0, duration_s)
        ]}
        for i in (0, 1)
    ]}


#: Between the runtime abort (1.5m) and the design limit (2.0m): the case the
#: old 0.8m threshold showed as green.
#: Short, so the committed snapshot stays small enough to read in review.
FIXTURE_SHOW = _pair_show(1.8, duration_s=0.2)


def test_validate_sends_the_live_default_limits():
    report = server.api_validate(FIXTURE_SHOW)
    assert report["limits"] == asdict(DEFAULT_LIMITS)


def test_generate_sends_the_live_default_limits():
    res = server.api_generate(server.GenerateRequest(shape="circle", num_drones=3, duration=30.0, rate=2.0))
    assert res["validation"]["limits"] == asdict(DEFAULT_LIMITS)


def test_the_design_separation_is_the_line_the_validator_actually_fails_on():
    """The UI shows separation green only at >= design_separation_m. That must
    be the same line the validator draws, or the panel and the verdict disagree."""
    limits = DEFAULT_LIMITS
    just_under = server.api_validate(_pair_show(limits.design_separation_m - 0.1))
    at_limit = server.api_validate(_pair_show(limits.design_separation_m))

    assert just_under["status"] == "FAILED"
    assert just_under["violations"]["proximity"]
    assert at_limit["status"] == "PASSED"


def test_frontend_fixture_is_the_live_validate_response():
    live = json.loads(json.dumps(server.api_validate(FIXTURE_SHOW)))
    on_disk = json.loads(FIXTURE.read_text())
    assert on_disk == live, (
        f"{FIXTURE.name} no longer matches /api/validate; the frontend's SafetyPanel tests "
        "are rendering a stale contract. Regenerate it (see this module's docstring)."
    )


if __name__ == "__main__":
    FIXTURE.write_text(json.dumps(server.api_validate(FIXTURE_SHOW), indent=2) + "\n")
    print(f"wrote {FIXTURE}")
