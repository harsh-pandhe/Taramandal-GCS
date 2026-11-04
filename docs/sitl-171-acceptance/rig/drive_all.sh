#!/usr/bin/env bash
# Sequential, one fresh container per run; each run commits and pushes before the next starts.
S=$HOME/Desktop/tara-sitl-scratch-171acc; cd $S
RUNS=(
  "nominal-2 nominal tara"
  "nominal-3 nominal tara"
  "takeover-dwell takeover_dwell tara"
  "takeover-landing takeover_landing tara"
  "link-kill link_kill tara"
  "reconnect reconnect tara"
  "reject reject tara"
  "cancel cancel tara"
  "gcs-kill gcs_kill decoupled"
  "stream-drop stream_drop tara"
  "stream-drop-hover stream_drop_hover tara"
  "armed-silent armed_silent tara"
  "guard guard tara unsigned"
  "abort-climb abort_climb tara"
)
for spec in "${RUNS[@]}"; do
  set -- $spec
  echo "=== $(date -u +%T) start $1"
  ./run_one.sh "$@"; rc=$?
  echo "=== $(date -u +%T) end $1 rc=$rc"
  if [ $rc = 3 ]; then echo "SLOT TAKEN - stopping"; break; fi
  ./push.sh
done
echo "=== ALL DONE $(date -u +%T)"
