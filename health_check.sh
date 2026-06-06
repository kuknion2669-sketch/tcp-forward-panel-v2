#!/bin/bash
cd /root/tcp-panel-v2
echo "=== Python syntax check ==="
for f in *.py; do
  python3 -m py_compile "$f" 2>&1 && echo "OK: $f" || echo "FAIL: $f"
done
echo "=== File listing ==="
ls -la *.py templates/*.html
echo "=== Git status ==="
git status --short
echo "=== Routes ==="
grep -n "^@app.route" panel.py
