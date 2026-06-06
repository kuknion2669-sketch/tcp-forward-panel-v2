#!/bin/bash
# Kill any existing v14 process
kill $(ps aux | grep 'python3 /root/tcp-panel-v2/panel.py' | grep -v grep | awk '{print $2}') 2>/dev/null
sleep 1
cd /root/tcp-panel-v2 && nohup python3 panel.py > /root/panel-v2.log 2>&1 &
sleep 2
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8081/
echo "=== PID ==="
ps aux | grep 'panel-v2' | grep -v grep | awk '{print $2}'
echo "=== Test login ==="
curl -s -c /tmp/cjar_v2 -b /tmp/cjar_v2 http://localhost:8081/login -d 'username=admin&password=admin123' -o /dev/null -w '%{http_code} '
curl -s -b /tmp/cjar_v2 http://localhost:8081/ | head -5
