#!/usr/bin/env bash
# Playwright E2E (tara-web) gate — full suite, including visual regression.
#
# Reuses the existing packages/tara-web/e2e/run-in-docker.mjs harness rather
# than a second Playwright Docker setup: that script already runs the exact
# pinned mcr.microsoft.com/playwright:v<version>-jammy image the committed
# -linux snapshot baselines were rendered by, reading the version from
# package-lock.json so it can never drift from what's actually installed.
# This is the same image .github/workflows/ci.yml's test-e2e job's
# `container:` directive uses — CI just runs it via a GitHub-hosted
# container job instead of `docker run`, same image either way.
#
# Requires Docker.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ci_log "tara-web — Playwright E2E via Docker (packages/tara-web/e2e/run-in-docker.mjs)"
(cd packages/tara-web && npm run test:e2e:docker)

ci_log "tara-studio-web — Playwright visual baseline via Docker (packages/tara-studio-web/e2e/run-in-docker.mjs)"
(cd packages/tara-studio-web && npm run test:e2e:docker)
