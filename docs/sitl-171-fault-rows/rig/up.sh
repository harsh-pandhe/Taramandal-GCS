#!/usr/bin/env bash
# Inside the container: the documented stack command for one acceptance run.
# Usage: up.sh RUNID [unsigned]
RUNID=$1; D=/scratch/out/$RUNID
cd /work
export VIRTUAL_ENV=/venv PX4_AUTOPILOT_DIR=/home/px4/PX4-Autopilot
export GCS_CONFIG_PATH=/scratch/tara.toml STUDIO_SIGNING_KEY=/scratch/studio.pem
export GCS_OPERATORS_PATH=/scratch/operators.json GCS_STATE_PATH=$D/state.json
export GCS_AUDIT_PATH=$D/audit.jsonl GCS_COMMAND_AUDIT_PATH=$D/cmd_audit.jsonl GCS_BLACKBOX_DIR=$D/blackbox
export CADENCE_LOG=$D/cadence.jsonl
EXTRA=""; [ "$2" = unsigned ] && EXTRA="--allow-unsigned"
exec ./tara up --backend px4 -n 1 $EXTRA
