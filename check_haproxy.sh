#!/bin/bash
# Watchdog: converge HAProxy to a single current process.
#
# WHY: HAProxy reload uses `-sf`, where the replaced master keeps serving its
# existing connections until they drain. With long-lived TCP forwards those
# old masters may never drain, so they pile up. The previous watchdog only
# killed processes that "no longer listen" -- but `-sf` remnants keep
# listening (SO_REUSEPORT), so nothing was ever cleaned (42 processes).
#
# NEW RULE: keep only the NEWEST haproxy process (highest PID); terminate any
# other process once it has been alive longer than the grace window. The
# newest process carries the current config. Older masters are drain
# remnants -- give them the grace window to drain, then force them down.
MAX_AGE_MIN=10
LOG=/root/panel-v2.log
NEWEST=$(pgrep -x haproxy 2>/dev/null | sort -n | tail -1)
for pid in $(pgrep -x haproxy 2>/dev/null); do
  [ "$pid" = "$NEWEST" ] && continue
  if ! kill -0 "$pid" 2>/dev/null; then continue; fi
  age=$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')
  [ -n "$age" ] || continue
  if [ "$age" -gt $((MAX_AGE_MIN * 60)) ]; then
    echo "$(date '+%F %T') watchdog: killing stale haproxy pid $pid (age ${age}s, newest=$NEWEST)" >> "$LOG"
    kill -TERM "$pid" 2>/dev/null || true
    sleep 5
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
  fi
done
exit 0
