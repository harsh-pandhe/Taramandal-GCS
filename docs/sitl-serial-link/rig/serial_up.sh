#!/usr/bin/env bash
# One drone on PX4 SITL (headless), reached either on UDP (MODE=udp) or through the virtual serial bridge (MODE=serial BAUD=n).
. $HOME/Desktop/tara-t1-scratch/bin/env.sh
unset DISPLAY
export PX4_AUTOPILOT_DIR=$HOME/PX4-Autopilot MAMBA_ROOT_PREFIX=$HOME/tara-t2/root
export CADENCE_LOG=$HOME/tara-t2/serial_runs/${RUN:?}/cadence.jsonl
mkdir -p $(dirname $CADENCE_LOG); : > $CADENCE_LOG
cd $HOME/Desktop/tara-wt-serial
rm -f $T/state.json; rm -rf $T/vault $T/blackbox
if [ "${MODE:-udp}" = serial ]; then
  python3 tools/serial_bridge.py --link /tmp/tara-vserial0 --udp-listen 14540 --baud ${BAUD:?} > $(dirname $CADENCE_LOG)/bridge.log 2>&1 &
  echo $! > $(dirname $CADENCE_LOG)/bridge.pid
  sleep 1
  export GCS_VEHICLE_ADDRESSES="0=serial:///tmp/tara-vserial0:${BAUD}"
fi
exec $HOME/tara-t2/bin/micromamba run -n t2 ./tara up --backend px4 -n 1
