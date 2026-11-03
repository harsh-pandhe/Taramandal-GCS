#!/usr/bin/env bash
pkill -f "launch_fleet.s[h]"; sleep 3
bash /work/sim/px4/stop_fleet.sh > /dev/null 2>&1; sleep 2
pkill -f "mavsdk_server -p 5009[9]"
cd /work && PX4_AUTOPILOT_DIR=/home/px4/PX4-Autopilot PATH=/usr/bin:$PATH nohup bash sim/px4/launch_fleet.sh -n 1 -h > /scratch/fleet.log 2>&1 &
