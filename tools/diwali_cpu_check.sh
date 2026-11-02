#!/usr/bin/env bash
# Is this machine quiet enough to take a Gazebo / SITL measurement? (docs/diwali-runbook.md)
#
#   tools/diwali_cpu_check.sh            check once; exit 0 if idle, 1 if busy
#   tools/diwali_cpu_check.sh --wait     poll until idle (prints a line every 5 minutes)
#
# Gazebo's real-time factor falls with host CPU contention: a foreign job dropped a run to RTF
# 0.4 once. A run taken on a busy machine is not evidence, so this is a gate, not advice.
#
# Idle means: no tara-sitl-* container running, and the 1-minute load average below LOAD_MAX
# (default 4, the threshold the #172 runs used; the SITL stack itself adds about 8 cores at 10
# drones, so judge the load BEFORE starting it). The busiest processes are listed so you can see
# who is responsible.
LOAD_MAX=${LOAD_MAX:-4}
WAIT=0; [ "${1:-}" = "--wait" ] && WAIT=1

busy_reason() {
  local names load
  names=$(docker ps --format '{{.Names}}' 2>/dev/null | grep '^tara-sitl-' | tr '\n' ' ')
  [ -n "$names" ] && { echo "SITL container running: $names"; return 0; }
  load=$(cut -d' ' -f1 /proc/loadavg)
  if awk -v l="$load" -v m="$LOAD_MAX" 'BEGIN { exit !(l >= m) }'; then
    echo "1-min load average $load >= $LOAD_MAX ($(nproc) cores)"; return 0
  fi
  return 1
}

last_note=0
while true; do
  if reason=$(busy_reason); then
    now=$(date +%s)
    if [ $((now - last_note)) -ge 300 ]; then
      echo "BUSY: $reason"
      ps -eo pcpu,etime,args --sort=-pcpu | sed -n 2,6p | cut -c1-140
      last_note=$now
    fi
    [ "$WAIT" = 1 ] || exit 1
    sleep 15
  else
    echo "IDLE: load $(cut -d' ' -f1-3 /proc/loadavg) on $(nproc) cores, no tara-sitl-* container"
    exit 0
  fi
done
