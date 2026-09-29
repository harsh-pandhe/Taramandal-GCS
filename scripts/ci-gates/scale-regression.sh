#!/usr/bin/env bash
# Scale Regression (100 drones) gate — the exact command
# .github/workflows/ci.yml's scale-regression job runs.
#
# Flies 100 drones through the real DroneManager sim backend and fails on
# any proximity or geofence breach. tools/benchmark_fleet_scale.py is not
# used: it reports breaches but always exits 0.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ci_log "100-drone show, zero safety breaches"
pytest packages/tara-gcs/tests/test_sim_backend.py::test_100_drone_sim_show_completes_with_zero_safety_violations -v --durations=0
