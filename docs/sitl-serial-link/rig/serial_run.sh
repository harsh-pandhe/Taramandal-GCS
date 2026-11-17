#!/usr/bin/env bash
# serial_run.sh RUN MODE [BAUD]: fresh stack, the whole acceptance sequence, stack down. Output in ~/tara-t2/serial_runs/RUN*.
RUN=$1; MODE=$2; BAUD=$3; export PX4_LINK_RATE=$4 PX4_LINK_STREAMS=$5   # optional: cap PX4's own link to this many bytes a second (like MAV_n_RATE) and give it an explicit NAME:Hz stream set
# Signal a recorded PID only if it still is our bridge: a stale PID file may name a recycled, unrelated process.
killbridges() {
  for f in $HOME/tara-t2/serial_runs/*/bridge.pid; do
    [ -f "$f" ] || continue
    pid=$(cat "$f")
    if [ -r "/proc/$pid/cmdline" ] && tr '\0' ' ' < "/proc/$pid/cmdline" | grep -q "tools/serial_bridge.py"; then kill "$pid" 2>/dev/null; fi
  done
  rm -f /tmp/tara-vserial0
}
bash $HOME/tara-t2/t2_down.sh >/dev/null 2>&1; killbridges; sleep 1
export RUN MODE BAUD
setsid nohup $HOME/tara-t2/serial_up.sh > $HOME/tara-t2/serial_runs/$RUN.stack.log 2>&1 < /dev/null &
source $HOME/Desktop/taramandal/.venv/bin/activate
cd $HOME/Desktop/tara-wt-serial
python $HOME/tara-t2/serial_drive.py $RUN $MODE > $HOME/tara-t2/serial_runs/$RUN.drive.log 2>&1
bash $HOME/tara-t2/t2_down.sh >/dev/null 2>&1; killbridges
echo "=== done $RUN $(date +%T)" >> $HOME/tara-t2/serial_runs/all.log
