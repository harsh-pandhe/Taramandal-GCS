#!/usr/bin/env bash
# Lint & Typecheck gate — the exact commands .github/workflows/ci.yml's
# lint-and-typecheck job runs. This script IS the job body: the workflow
# calls it instead of duplicating these lines, so there is one place that
# defines what this gate means.
#
# Assumes: `pip install -r requirements.txt -r requirements-dev.txt` has
# already been run (locally: once per dependency change; in CI: every run,
# via actions/setup-python's pip cache).
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ci_log "ruff check ."
ruff check .

ci_log "mypy packages/tara-core"
mypy packages/tara-core

# Warnings are reported but not fatal: tara-web carries 3 pre-existing
# react-hooks/exhaustive-deps warnings in EarthView.jsx (see ci.yml).
ci_log "oxlint (tara-web)"
(cd packages/tara-web && npm ci --no-audit --no-fund && npm run lint)

ci_log "oxlint (tara-studio-web)"
(cd packages/tara-studio-web && npm ci --no-audit --no-fund && npm run lint)
