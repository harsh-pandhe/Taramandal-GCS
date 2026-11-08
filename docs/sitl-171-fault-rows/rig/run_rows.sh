#!/usr/bin/env bash
cd $HOME/Desktop/tara-sitl-scratch-171rows
for spec in "denied-land-3 denied_land_termination_clean" "cold-start-1 cold_start"; do
  set -- $spec; echo "=== $(date -u +%T) start $1"; NOCOMMIT=1 ./run_one.sh $1 $2 tara; echo "=== $(date -u +%T) end $1 rc=$?"
done
echo "=== ROWS DONE"
