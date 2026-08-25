#!/bin/bash
# Daily backup of the panel DB + HAProxy config. Keeps the last 7 copies.
set -e
BK=/root/panel-backups
mkdir -p "$BK"
TS=$(date +%Y%m%d_%H%M%S)
if [ -f /root/traffic.db ]; then
  cp /root/traffic.db "$BK/traffic_$TS.db"
  gzip -f "$BK/traffic_$TS.db"
fi
if [ -f /etc/haproxy/haproxy.cfg ]; then
  cp /etc/haproxy/haproxy.cfg "$BK/haproxy_$TS.cfg"
  gzip -f "$BK/haproxy_$TS.cfg"
fi
cd "$BK"
ls -1t traffic_*.db.gz 2>/dev/null | tail -n +8 | xargs -r rm -f --
ls -1t haproxy_*.cfg.gz 2>/dev/null | tail -n +8 | xargs -r rm -f --
echo "backup done: $TS"
