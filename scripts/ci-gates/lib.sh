#!/usr/bin/env bash
# Shared helpers for the scripts/ci-gates/*.sh gates and the ./scripts/ci
# runner. Sourced, not executed directly.

set -euo pipefail

# Repo root, regardless of the caller's cwd — every gate script cds here first
# so `working-directory:`-scoped commands (npm ci in a package dir, etc.) match
# what .github/workflows/ci.yml does relative to the checkout root. This file
# lives at scripts/ci-gates/lib.sh, two levels under the repo root.
CI_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$CI_REPO_ROOT"

if [ -t 1 ]; then
  CI_C_RED=$'\033[31m'; CI_C_GREEN=$'\033[32m'; CI_C_YELLOW=$'\033[33m'; CI_C_RESET=$'\033[0m'
else
  CI_C_RED=""; CI_C_GREEN=""; CI_C_YELLOW=""; CI_C_RESET=""
fi

ci_log() { echo "${CI_C_YELLOW}[$(basename "$0")]${CI_C_RESET} $*"; }
ci_pass() { echo "${CI_C_GREEN}PASS${CI_C_RESET} $*"; }
ci_fail() { echo "${CI_C_RED}FAIL${CI_C_RESET} $*"; }
