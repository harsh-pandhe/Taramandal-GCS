"""Issue #160: Studio-built shows must certify (0 jerk violations) through
the real /api/export, not just pass validate_dynamics in isolation.

Fixtures (tests/fixtures/issue_160_reference_shows/*.json.gz) are real
tara_studio_web/src/engine/sequencer.js output - built with
buildSequencedShow at MIN_CERTIFIABLE_RATE_HZ (10 Hz), gzip-committed since a
100-drone show's full waypoint JSON runs 20+ MB uncompressed. Regenerate with
packages/tara-studio-web/.gen_ref_fixtures_160.mjs (a throwaway script kept
out of the repo; copy it back in locally to rerun) if sequencer.js's pacing
changes.

The five shapes are a representative reconstruction of the issue's own
"isolated export of main" evidence table (demo 5->4 at 6 and 24 drones,
circle->grid at 24, grid alone at 100, heart->grid at 100) - the exact scene
parameters Harsh used to gather that table weren't preserved, so these use
this codebase's own established test conventions instead of guessing at a
byte-identical original. What matters for this test is that each shape
genuinely failed jerk certification before issue #160 (confirmed directly,
not assumed - see the PR description) and now passes through the same real
export path an operator's "Export" button calls.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from fastapi.testclient import TestClient

from tara_studio.server import app
from tara_studio.validator import certify_feasibility_report

client = TestClient(app)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "issue_160_reference_shows"

REFERENCE_SHOWS = [
    "demo_5_4_n6",
    "demo_5_4_n24",
    "circle_grid_n24",
    "grid_n100",
    "heart_grid_n100",
]


def _load(name: str) -> dict:
    with gzip.open(FIXTURES_DIR / f"{name}.json.gz", "rt") as f:
        return json.load(f)


def test_reference_show_fixtures_exist():
    # Fails loudly and specifically if the gzip fixtures are ever missing or
    # renamed, rather than every certification test below failing on a
    # generic file-not-found.
    for name in REFERENCE_SHOWS:
        assert (FIXTURES_DIR / f"{name}.json.gz").exists(), name


def test_each_reference_show_certifies_through_the_real_export_endpoint():
    # /api/export calls certify_feasibility_report internally and fails
    # closed (412) on anything short of full certification for a flight
    # (non-preview) export - a 200 here *is* the certification proof, the
    # same gate an operator's own "Export" button goes through.
    for name in REFERENCE_SHOWS:
        choreo = _load(name)
        res = client.post("/api/export", json={"choreo": choreo, "format": "json"})
        assert res.status_code == 200, f"{name}: {res.status_code} {res.json()}"
        body = res.json()
        assert body["preview_only"] is False, name


def test_each_reference_show_has_zero_jerk_violations():
    # certify_feasibility_report is the exact function /api/export calls
    # internally (server.py's own canonical certification gate) - called
    # directly here only to see the metrics/violations detail the export
    # response itself doesn't echo back, not a separate/looser check.
    for name in REFERENCE_SHOWS:
        choreo = _load(name)
        is_certified, report = certify_feasibility_report(choreo)
        assert is_certified, f"{name}: {report['summary']}"
        assert report["violations"]["jerk"] == [], f"{name}: {report['violations']['jerk']}"
        assert report["metrics"]["max_jerk_m_s3"] <= 20.0, name
