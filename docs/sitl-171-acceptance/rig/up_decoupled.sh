#!/usr/bin/env bash
# Same fleet and same GCS command line as `tara up`, but the GCS is not a child of the
# supervisor: tara's supervisor stops the whole stack when gcs-api dies (process.py), which
# would take PX4 down with it and make a dead-GCS test meaningless. Usage: up_decoupled.sh RUNID
RUNID=$1; D=/scratch/out/$RUNID
cd /work
export VIRTUAL_ENV=/venv PX4_AUTOPILOT_DIR=/home/px4/PX4-Autopilot
PATH=/usr/bin:$PATH nohup bash sim/px4/launch_fleet.sh -n 1 -h > $D/fleet.log 2>&1 &
sleep 40
export PYTHONPATH=/work/packages/tara-gcs:/work/packages/tara-core:/work/packages/tara-sim
export GCS_PORT=8090 NUM_DRONES=1 VEHICLE_BACKEND=px4 GCS_ALLOW_UNSIGNED=0
export GCS_CONFIG_PATH=/scratch/tara.toml STUDIO_SIGNING_KEY=/scratch/studio.pem
export GCS_OPERATORS_PATH=/scratch/operators.json GCS_STATE_PATH=$D/state.json
export GCS_AUDIT_PATH=$D/audit.jsonl GCS_COMMAND_AUDIT_PATH=$D/cmd_audit.jsonl GCS_BLACKBOX_DIR=$D/blackbox
export CADENCE_LOG=$D/cadence.jsonl
exec /venv/bin/python -m uvicorn tara_gcs.main:app --host 0.0.0.0 --port 8090 > $D/gcs.log 2>&1
