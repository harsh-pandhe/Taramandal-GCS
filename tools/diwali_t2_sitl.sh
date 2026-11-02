#!/usr/bin/env bash
# One T2 run of a Diwali show on PX4 SITL + Gazebo, in a fresh container (see docs/diwali-runbook.md).
#
#   tools/diwali_t2_sitl.sh <drones> <label> [show.json]
#   tools/diwali_t2_sitl.sh 3 rehearsal examples/diwali/segments/diwali_3drone_segment.json
#
# Why a container: this host has no PX4 build and no Gazebo, so `./tara up --backend px4` only
# works inside the taramandal-px4-sitl image. Docker Desktop's `--network host` is the VM's
# network, not the host's, so the GCS and the measuring tool both run inside the container.
#
# Gazebo's real-time factor depends on host CPU. This script refuses to start unless
# tools/diwali_cpu_check.sh says the machine is idle (no other tara-sitl-* container, low load),
# and it prints the host load before and after the run.
#
# Needs: Docker Desktop running, the image, node on the host (copied in: the image has none),
# `npm install` done in packages/tara-web and packages/tara-studio-web, and a venv
# ($VENV, default <repo>/.venv; its Python must match the image's 3.12).
set -uo pipefail
N=${1:?usage: $0 <drones> <label> [show.json]}
LABEL=${2:?usage: $0 <drones> <label> [show.json]}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHOW=${3:-}
IMAGE=${IMAGE:-taramandal-px4-sitl:v1.16.2}
C=${CONTAINER:-tara-sitl-demo}
# Docker Desktop refuses bind mounts from /tmp: keep scratch under $HOME.
SCRATCH=${SCRATCH:-$HOME/tara-sitl-demo-scratch}
VENV=${VENV:-$ROOT/.venv}
mkdir -p "$SCRATCH/runs"

# Hard gate, never overridable: another tab flying on Gazebo would be perturbed by this run (and vice versa).
others=$(docker ps --format '{{.Names}}' | grep '^tara-sitl-' | grep -v "^$C$" | tr '\n' ' ')
if [ -n "$others" ]; then
  echo "REFUSING: another SITL container is running ($others). Wait for it to stop; FORCE_BUSY does not override this." >&2
  exit 3
fi

# Gate: a quiet CPU. FORCE_BUSY=1 runs anyway on a loaded (but SITL-free) host; the run then says it is not evidence.
if ! "$ROOT/tools/diwali_cpu_check.sh"; then
  if [ "${FORCE_BUSY:-0}" != 1 ]; then
    echo "REFUSING: machine is not idle (see above). Wait (tools/diwali_cpu_check.sh --wait) or set FORCE_BUSY=1 for a rehearsal that must not be counted." >&2
    exit 3
  fi
  echo "[$LABEL] FORCE_BUSY=1: this run is NOT valid evidence" >&2
fi

# One-time setup in $SCRATCH: signing key, trusted-key config, an operator, a passphrase.
export VIRTUAL_ENV="$VENV"
if [ ! -f "$SCRATCH/studio.pem" ]; then
  keyline=$("$ROOT/tara" keygen "$SCRATCH/studio.pem" --signer "Diwali demo" 2>&1 | grep 'public_key')
  python3 - "$ROOT/tara.toml" "$SCRATCH/tara.toml" "$keyline" <<'PY'
import sys
src, dst, keyline = sys.argv[1:]
text = open(src).read().replace("trusted_keys = [", "trusted_keys = [\n" + keyline.rstrip(), 1)
open(dst, "w").write(text)
PY
fi
if [ ! -f "$SCRATCH/.passphrase" ]; then
  head -c 12 /dev/urandom | base64 > "$SCRATCH/.passphrase"; chmod 600 "$SCRATCH/.passphrase"
  rm -f "$SCRATCH/operators.json"
fi
PASS=$(cat "$SCRATCH/.passphrase")
if [ ! -f "$SCRATCH/operators.json" ]; then
  PYTHONPATH="$ROOT/packages/tara-gcs:$ROOT/packages/tara-core" "$VENV/bin/python" -c \
    'import sys; from pathlib import Path; from tara_gcs.auth import OperatorStore, Role; OperatorStore(Path(sys.argv[1])).add("demo", Role.FLIGHT_COMMANDER, sys.argv[2])' \
    "$SCRATCH/operators.json" "$PASS"
  chmod 600 "$SCRATCH/operators.json"
fi

cat > "$SCRATCH/up.sh" <<'EOF'
#!/usr/bin/env bash
cd /work
export VIRTUAL_ENV=/venv PX4_AUTOPILOT_DIR=/home/px4/PX4-Autopilot
export GCS_CONFIG_PATH=/scratch/tara.toml STUDIO_SIGNING_KEY=/scratch/studio.pem
export GCS_OPERATORS_PATH=/scratch/operators.json GCS_STATE_PATH=/scratch/state.json
export GCS_AUDIT_PATH=/scratch/audit.jsonl GCS_COMMAND_AUDIT_PATH=/scratch/cmd_audit.jsonl
rm -f /scratch/state.json
exec ./tara up --backend px4 -n "$1"
EOF
chmod +x "$SCRATCH/up.sh"

# A fresh container every run, so nothing from the last run (mission directory, state) hides a regression.
docker rm -f "$C" >/dev/null 2>&1
LOAD_BEFORE=$(cut -d' ' -f1-3 /proc/loadavg)
echo "[$LABEL] host load before: $LOAD_BEFORE"
docker run -d --name "$C" --network host -v "$SCRATCH":/scratch -v "$ROOT":/work -v "$VENV":/venv:ro \
  "$IMAGE" sleep infinity >/dev/null || { echo "docker run failed: is Docker Desktop running and the image present?" >&2; exit 2; }
docker exec -u root "$C" mkdir -p /usr/local/lib/node_modules
docker cp -q "$(command -v node)" "$C:/usr/local/bin/node"
docker cp -q "$(dirname "$(readlink -f "$(command -v npm)")")/.." "$C:/usr/local/lib/node_modules/npm"
docker exec -u root "$C" bash -c 'ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm'

docker exec -d "$C" bash -c "/scratch/up.sh $N > /scratch/up_$LABEL.log 2>&1"
c=
for i in $(seq 1 120); do
  sleep 2
  c=$(docker exec "$C" curl -s localhost:8090/api/status 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["connected"])' 2>/dev/null)
  [ "$c" = "$N" ] && break
done
echo "[$LABEL] stack up: connected=$c of $N after $((i * 2))s"
[ "$c" = "$N" ] || { echo "stack did not come up; see $SCRATCH/up_$LABEL.log" >&2; exit 2; }
sleep 20  # let every drone's EKF settle before the upload

SHOW_ARGS=(--drones "$N")
[ -n "$SHOW" ] && SHOW_ARGS=(--show "/work/${SHOW#"$ROOT"/}")
docker exec -w /work -e TARA_PASSPHRASE="$PASS" "$C" \
  python3 tools/sitl_planned_vs_actual.py run "${SHOW_ARGS[@]}" --operator demo --out /scratch/runs \
  --label "$LABEL" --commit "$(git -C "$ROOT" rev-parse HEAD)" | tee "$SCRATCH/runs/$LABEL.stdout"
rc=${PIPESTATUS[0]}
echo "[$LABEL] host load before/after: $LOAD_BEFORE / $(cut -d' ' -f1-3 /proc/loadavg)"
echo "[$LABEL] result: $SCRATCH/runs/$LABEL.json   stop the container: docker rm -f $C"
exit "$rc"
