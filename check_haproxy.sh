#!/bin/bash
# Watchdog: kill stale HAProxy processes that no longer listen on any port
# and have been draining for more than 10 minutes.
MAX_AGE_MIN=10
LOG=/root/panel-v2.log
for pid in $(pgrep -x haproxy 2>/dev/null); do
  if ss -tlnp 2>/dev/null | grep -q "pid=$pid,"; then
    continue
  fi
  age=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
  [ -n "$age" ] || continue
  if [ "$age" -gt $((MAX_AGE_MIN * 60)) ]; then
    echo "$(date '+%F %T') watchdog: killing stale haproxy pid $pid (age ${age}s)" >> "$LOG"
    kill "$pid" 2>/dev/null || kill -9 "$pid" 2>/dev/null
  fi
done
exit 0
