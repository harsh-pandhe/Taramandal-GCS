#!/usr/bin/env bash
# Posts a `taramandal/local-ci` GitHub commit status for HEAD, honestly
# reflecting the result the caller actually observed — this script never
# decides pass/fail itself, it only reports what ./scripts/ci already found.
#
# Usage: scripts/ci-gates/report-status.sh <exit-code-from-./scripts/ci>
#
# Requires `gh` authenticated with a token that has at least
# "Commit statuses: Read and write" for this repository (a fine-grained PAT
# scoped to this repo only, not a classic broad-`repo`-scope token). No
# token is read, stored, or logged by this script — it uses whatever `gh`
# is already authenticated with (`gh auth token`), so this script itself
# never has a credential to leak.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

rc="${1:?usage: report-status.sh <exit-code>}"

if [ "$rc" -eq 0 ]; then
  state="success"
else
  state="failure"
fi

sha="$(git rev-parse HEAD)"
who="${USER:-unknown}@$(hostname -s 2>/dev/null || hostname)"
when="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# GitHub caps a status description at 140 characters.
description="local-ci: ${state} on ${who} at ${when}"

repo="$(gh repo view --json nameWithOwner --jq .nameWithOwner)"

gh api "repos/${repo}/statuses/${sha}" \
  -f "state=${state}" \
  -f "context=taramandal/local-ci" \
  -f "description=${description}" \
  >/dev/null

echo "Reported taramandal/local-ci: ${state} for ${sha:0:8} (${repo})"
