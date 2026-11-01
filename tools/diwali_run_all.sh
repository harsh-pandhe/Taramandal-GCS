#!/usr/bin/env bash
# Builds the Diwali storyboards and certifies them through the real Studio
# /api/export. Starts its own backend on a spare port and stops it afterwards.
#
#   tools/diwali_run_all.sh [out_dir]          (run from anywhere; uses .venv if present)
#   DIWALI_SIZES="35 100" tools/diwali_run_all.sh   # only these storyboard_<name>.json files
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/dist/diwali}"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
PORT="${DIWALI_API_PORT:-8011}"
export PYTHONPATH="$ROOT/packages/tara-core:$ROOT/packages/tara-gcs:$ROOT/packages/tara-sim:$ROOT/packages/tara-studio:$ROOT/apps:$ROOT/apps/tara"

"$PY" -m uvicorn tara_studio.server:app --port "$PORT" --log-level warning &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$PORT/api/signing" >/dev/null && break
  sleep 0.5
done

status=0
for n in ${DIWALI_SIZES:-3 35 35_lettering 100 500}; do
  echo "=== storyboard_$n ==="
  node "$ROOT/tools/diwali_build.mjs" --storyboard "$ROOT/examples/diwali/storyboard_$n.json" \
    --api "http://127.0.0.1:$PORT" --out "$OUT" --tag "$n" || status=$?
done
exit $status
