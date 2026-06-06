#!/bin/bash
cd /root/tcp-panel-v2
rm -rf __pycache__ *.pyc
echo "=== Python syntax ==="
for f in *.py; do
  python3 -m py_compile "$f" 2>&1 && echo "OK: $f" || echo "FAIL: $f"
done
echo "=== Restart ==="
PID=$(ps aux | grep 'python3.*panel-v2' | grep -v grep | awk '{print $2}')
[ -n "$PID" ] && kill $PID 2>/dev/null
sleep 1
nohup python3 panel.py > /root/panel-v2.log 2>&1 &
sleep 2
echo "=== Git ==="
git add -A
git status --short
git commit -m 'v14 fixes: batch_add, logging, .gitignore, port config' --allow-empty
git push 2>&1 | tail -3
