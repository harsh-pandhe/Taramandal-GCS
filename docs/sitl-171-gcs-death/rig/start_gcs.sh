#!/usr/bin/env bash
# Usage: start_gcs.sh 0|1 (lifeline off/on)
pkill -f "[g]cs_run.py"; pkill -f "mavsdk_server -p 5005[1]"; sleep 2
rm -f /scratch/state.json
cd /work
export PYTHONPATH=/work/packages/tara-gcs:/work/packages/tara-core:/work/packages/tara-sim
export GCS_PORT=8090 NUM_DRONES=1 VEHICLE_BACKEND=px4 GCS_ALLOW_UNSIGNED=1 LIFELINE=${1:-1}
export GCS_OPERATORS_PATH=/scratch/operators.json GCS_STATE_PATH=/scratch/state.json GCS_AUDIT_PATH=/scratch/audit.jsonl GCS_COMMAND_AUDIT_PATH=/scratch/cmd_audit.jsonl
nohup /venv/bin/python -W ignore /scratch/gcs_run.py > /scratch/gcs.log 2>&1 &
for i in $(seq 1 40); do sleep 2; curl -s localhost:8090/api/status 2>/dev/null | grep -q '"connected":1' && { echo "gcs connected after $((i*2))s lifeline=$LIFELINE"; exit 0; }; done; echo "gcs NOT connected"; tail -5 /scratch/gcs.log; exit 1
