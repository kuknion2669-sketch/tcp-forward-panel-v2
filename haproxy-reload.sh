#!/bin/sh
# HAProxy seamless reload (master-worker mode).
#
# The master owns listening sockets and hands them to the new worker, so a
# reload has no bind conflict and no listening gap. This script only starts the
# reload; panel/haproxy_ctl.py separately verifies that the new worker is alive
# and that every expected frontend is present before marking the config applied.
set -e
CFG=/etc/haproxy/haproxy.cfg

/usr/sbin/haproxy -c -f "$CFG" -q || exit 1

MASTER=$(systemctl show -p MainPID --value haproxy 2>/dev/null | tr -d ' \n')
if [ -z "$MASTER" ] || [ "$MASTER" = "0" ] || ! kill -0 "$MASTER" 2>/dev/null; then
    echo "haproxy-reload: no live master (MainPID='$MASTER')" >&2
    exit 1
fi

if pgrep -P "$MASTER" -x haproxy >/dev/null 2>&1; then
    kill -USR2 "$MASTER"
    exit 0
fi

# One-time migration from single-process mode. Detached so systemd cannot
# deadlock on a reload job that restarts its own unit.
setsid sh -c 'sleep 1; systemctl restart haproxy' >/dev/null 2>&1 &
exit 0
