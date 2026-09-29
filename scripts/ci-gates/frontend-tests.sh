#!/usr/bin/env bash
# Frontend Tests gate — the exact commands .github/workflows/ci.yml's
# frontend-tests job runs.
#
# The 2 "visual regression" snapshot tests are excluded here: they run in
# test-e2e.sh, against the pinned Docker image whose exact browser/GL build
# produced the committed -linux baselines. Running them again here, against
# this job's plain chromium install, would just diff against baselines
# rendered by a different browser build.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ci_log "tara-studio-web — Vitest + build"
(cd packages/tara-studio-web && npm ci --no-audit --no-fund && npm test && npm run build)

ci_log "tara-web — build"
(cd packages/tara-web && npm ci --no-audit --no-fund && npm run build)

ci_log "tara-web — install Playwright Chromium"
(cd packages/tara-web && npx playwright install chromium)

ci_log "tara-web — Playwright E2E (excluding visual regression)"
(cd packages/tara-web && npx playwright test --grep-invert "visual regression")
