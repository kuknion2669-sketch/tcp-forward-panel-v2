#!/bin/bash
# HAProxy health watchdog.
#
# HISTORY: this script used to keep only the NEWEST haproxy PID and TERM/KILL
# every older one after a 10 min grace window. That was correct for the old
# model (single process + manual `-sf` reload, which left drain remnants
# behind), but it is DESTRUCTIVE in master-worker mode: the master is OLDER
# than its worker, so the watchdog would kill the master itself.
#
# NOW: only ensure HAProxy is actually alive - systemd owns the master, so
# there are no orphan processes left to reap.
LOG=/root/panel-v2.log

if pgrep -x haproxy >/dev/null 2>&1; then
    MASTER=$(systemctl show -p MainPID --value haproxy 2>/dev/null | tr -d ' \n')
    if [ -n "$MASTER" ] && [ "$MASTER" != "0" ] && ! kill -0 "$MASTER" 2>/dev/null; then
        echo "$(date '+%F %T') watchdog: master $MASTER not alive, restarting haproxy" >> "$LOG"
        systemctl restart haproxy
    fi
    exit 0
fi

echo "$(date '+%F %T') watchdog: no haproxy process, restarting haproxy" >> "$LOG"
systemctl restart haproxy
exit 0
