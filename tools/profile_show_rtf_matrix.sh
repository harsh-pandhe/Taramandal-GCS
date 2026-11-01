#!/usr/bin/env bash
# Runs the whole set of profiles behind docs/sim-rtf-profiling.md, one after another.
#
#   tools/profile_show_rtf_matrix.sh <dir with diwali_35/100/500.json> [out_dir]
#
# Each run waits for tara-sitl-* containers to stop first (the tool does this itself) and is
# repeated, up to 3 tries, if a SITL container showed up mid-run and the tool marked it invalid.
# Sequential on purpose: parallel runs would perturb each other and Gazebo's RTF on the same host.
# Takes about 1.5 hours; the 500-drone runs include a 400 s launch each.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SHOWS="${1:?usage: $0 <dir with diwali_*.json> [out_dir]}"
OUT="${2:-$ROOT/dist/rtf_profile}"
PY="${PYTHON:-$ROOT/.venv/bin/python}"
mkdir -p "$OUT"
cd "$ROOT"

run() {  # run <name> <args...>
  local name=$1; shift
  for try in 1 2 3; do
    echo "[$(date +%T)] START $name try $try"
    "$PY" tools/profile_show_rtf.py "$@" --out "$OUT/$name.json" > "$OUT/$name.log" 2>&1 || return 1
    valid=$("$PY" -I -c "import json,sys;print(json.load(open(sys.argv[1]))['valid'])" "$OUT/$name.json")
    echo "[$(date +%T)] END   $name valid=$valid"
    [ "$valid" = True ] && return 0
  done
  return 1
}

for n in 35 100 500; do run "steps_$n" "$SHOWS/diwali_$n.json"; done
for n in 35 100 500; do run "cprofile_$n" "$SHOWS/diwali_$n.json" --profiler cprofile; done
# Counterfactuals: the show-state snapshot switched off (this process only, no product edit).
run steps_100_nosnap "$SHOWS/diwali_100.json" --disable snapshot
run steps_500_nosnap "$SHOWS/diwali_500.json" --disable snapshot
# Controls: the synthetic benchmark show, stock (4 waypoints, 15 s) and dense (10 Hz, 216 s).
run syn500_stock --synthetic 500 --duration 15 --rate-hz 0
run syn500_dense --synthetic 500 --duration 216 --rate-hz 10
