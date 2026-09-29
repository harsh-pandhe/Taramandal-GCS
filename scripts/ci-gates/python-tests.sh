#!/usr/bin/env bash
# Python Tests gate — the exact command .github/workflows/ci.yml's
# python-tests job runs.
#
# The `slow` tests are covered elsewhere: the 100-drone show runs in
# scale-regression.sh, and the tara-up/down lifecycle test is excluded
# (hardcodes .venv/bin/python — see #46).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ci_log "pytest -p asyncio -m \"not slow\""
pytest -p asyncio -m "not slow"
