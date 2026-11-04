#!/usr/bin/env bash
S=$HOME/Desktop/tara-sitl-scratch-171acc; cd $S
RUNS=(
  "reconnect-settled reconnect_settled tara"
  "cancel-2 cancel tara"
  "armed-silent-2 armed_silent tara"
)
for spec in "${RUNS[@]}"; do
  set -- $spec
  echo "=== $(date -u +%T) start $1"
  ./run_one.sh "$@"; rc=$?
  echo "=== $(date -u +%T) end $1 rc=$rc"
  if [ $rc = 3 ]; then echo "SLOT TAKEN - stopping"; break; fi
  ./push.sh
done
echo "=== ALL DONE (extra) $(date -u +%T)"
