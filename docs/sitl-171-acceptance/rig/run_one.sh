#!/usr/bin/env bash
# Host side: ONE acceptance run in a brand-new container, removed as the last step.
# Usage: run_one.sh RUNID CASE [tara|decoupled] [unsigned]
RUNID=$1; CASE=$2; MODE=${3:-tara}; UNSIGNED=$4
C=tara-sitl-171acc; S=$HOME/Desktop/tara-sitl-scratch-171acc; W=$HOME/Desktop/tara-wt-171acc
OUTD=$S/out/$RUNID; DOCS=$W/docs/sitl-171-acceptance/$RUNID
cleanup() { docker rm -f $C >/dev/null 2>&1; }
trap cleanup EXIT
OTHER_SITL=$(docker ps --format '{{.Names}}' | grep '^tara-sitl-' | grep -v "^$C$" | tr '\n' ' ')
if [ -n "$OTHER_SITL" ]; then echo "[$RUNID] SLOT TAKEN by: $OTHER_SITL - not flying"; exit 3; fi
docker rm -f $C >/dev/null 2>&1
rm -rf "$OUTD"; mkdir -p "$OUTD"
cp $S/inst/sitecustomize.py $W/packages/tara-gcs/sitecustomize.py   # untracked cadence logger, see inst/
COMMIT=$(git -C "$W" rev-parse HEAD)
START_ISO=$(date -u +%FT%TZ)
LOAD_BEFORE=$(cut -d' ' -f1-3 /proc/loadavg)
OTHERS=$(docker ps --format '{{.Names}}' | grep -v "^$C$" | tr '\n' ' ')
echo "[$RUNID] case=$CASE mode=$MODE commit=${COMMIT:0:7} load_before=$LOAD_BEFORE other_containers=[$OTHERS]"
docker run -d --name $C --network host -v "$S":/scratch -v "$W":/work \
  -v "$HOME/Desktop/taramandal/.venv":/venv:ro taramandal-px4-sitl:v1.16.2 sleep infinity >/dev/null || exit 4
docker exec -u root $C mkdir -p /usr/local/lib/node_modules
docker cp -q /usr/bin/node $C:/usr/local/bin/node
docker cp -q /usr/lib/node_modules/npm $C:/usr/local/lib/node_modules/
docker exec -u root $C bash -c 'ln -sf /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm'
MICROSD=$(docker exec $C bash -c 'ls -d /home/px4/PX4-Autopilot/build/px4_sitl_default/rootfs/*/fs/microsd 2>/dev/null | wc -l')
PX4VER=$(docker exec $C bash -c 'git -C /home/px4/PX4-Autopilot describe --tags 2>/dev/null')
if [ "$MODE" = decoupled ]; then UPCMD="/scratch/up_decoupled.sh $RUNID"; else UPCMD="/scratch/up.sh $RUNID $UNSIGNED"; fi
docker exec -d $C bash -c "$UPCMD > $( [ "$MODE" = decoupled ] && echo /scratch/out/$RUNID/up.log || echo /scratch/out/$RUNID/up.log ) 2>&1"
UPOK=0
for i in $(seq 1 150); do
  sleep 2
  c=$(docker exec $C curl -s localhost:8090/api/status 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["connected"])' 2>/dev/null)
  [ "$c" = "1" ] && { UPOK=1; break; }
done
echo "[$RUNID] stack up: ok=$UPOK connected=$c after $((i*2))s"
sleep 10
LOAD_START=$(cut -d' ' -f1-3 /proc/loadavg)
if [ $UPOK = 1 ]; then
  timeout 600 docker exec -e OUT=/scratch/out/$RUNID $C /venv/bin/python -W ignore /scratch/scenarios.py "$CASE" > "$OUTD/scenario.log" 2>&1
  RC=$?
else RC=99; echo "stack did not come up" > "$OUTD/scenario.log"; fi
LOAD_END=$(cut -d' ' -f1-3 /proc/loadavg)
docker logs $C > "$OUTD/docker.log" 2>&1
docker exec $C bash -c 'cd /home/px4/PX4-Autopilot/build/px4_sitl_default/rootfs 2>/dev/null && tar czf - */log 2>/dev/null' > "$OUTD/px4_ulogs.tgz" 2>/dev/null
docker exec $C bash -c 'cd /tmp && tar czf - px4_*.log 2>/dev/null' > "$OUTD/px4_console.tgz" 2>/dev/null
N_ULOG=$(tar tzf "$OUTD/px4_ulogs.tgz" 2>/dev/null | grep -c '\.ulg$')
cleanup   # the container is gone before anything slow happens
echo "[$RUNID] container removed; scenario rc=$RC; ulogs=$N_ULOG"
gzip -f "$OUTD/docker.log" "$OUTD/cadence.jsonl" 2>/dev/null
rm -f "$W/packages/tara-gcs/sitecustomize.py"
python3 - "$OUTD/context.json" "$RUNID" "$CASE" "$MODE" "$UNSIGNED" "$COMMIT" "$START_ISO" "$LOAD_BEFORE" "$LOAD_START" "$LOAD_END" "$OTHERS" "$MICROSD" "$PX4VER" "$N_ULOG" "$RC" <<'PY'
import json, subprocess, sys, platform
p, rid, case, mode, unsigned, commit, start, l0, l1, l2, others, microsd, px4ver, nul, rc = sys.argv[1:]
img = subprocess.run(["docker", "image", "inspect", "taramandal-px4-sitl:v1.16.2", "--format", "{{.Id}}"], capture_output=True, text=True).stdout.strip()
dv = subprocess.run(["docker", "version", "--format", "{{.Server.Version}} on {{.Server.Os}}/{{.Server.Arch}}"], capture_output=True, text=True).stdout.strip()
nproc = subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip()
json.dump({"run_id": rid, "case": case, "stack": "tara up --backend px4 -n 1 (headless)" if mode == "tara" else "launch_fleet.sh -n 1 -h + uvicorn tara_gcs.main:app started outside the tara supervisor",
           "allow_unsigned": unsigned == "unsigned", "commit": commit, "started_utc": start, "host_load_before": l0, "host_load_at_start": l1, "host_load_at_end": l2,
           "other_running_containers": others.split(), "fresh_container": True, "microsd_dirs_before_stack": int(microsd or 0),
           "px4_version": px4ver, "image_id": img, "docker_server": dv, "host_cpus": nproc, "host_kernel": platform.release(),
           "headless": True, "gazebo_window": False, "rviz": False, "recording": False, "px4_ulogs_saved": int(nul or 0), "scenario_exit_code": int(rc)},
          open(p, "w"), indent=2)
PY
python3 $S/mk_record.py "$OUTD" "$OUTD/context.json"
rm -rf "$DOCS"; mkdir -p "$DOCS"
cp "$OUTD"/{run.json,result.json,timeline.csv,scenario.log,up.log,cmd_audit.jsonl,audit.jsonl,cadence.jsonl.gz,docker.log.gz,px4_ulogs.tgz,px4_console.tgz,fleet.log,gcs.log} "$DOCS"/ 2>/dev/null
du -sh "$DOCS" | sed "s/^/[$RUNID] saved: /"
if [ "${NOCOMMIT:-0}" != 1 ]; then
  git -C "$W" add "docs/sitl-171-acceptance/$RUNID" && git -C "$W" commit -q -m "docs(sitl): #171 acceptance run $RUNID ($CASE)

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>" && echo "[$RUNID] committed locally"
fi
