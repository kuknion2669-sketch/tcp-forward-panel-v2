#!/bin/bash
echo "=== Edit page ==="
curl -s -b /tmp/cjar_v2b http://localhost:8081/edit/0 | grep -c 'form method'
echo "=== index page (groups) ==="
curl -s -b /tmp/cjar_v2b http://localhost:8081/ | grep -c 'gc-item'
echo "=== all-view page ==="
curl -s -b /tmp/cjar_v2b http://localhost:8081/?view=all | grep -c 'checkAllBtn'
echo "=== haproxy page ==="
curl -s -b /tmp/cjar_v2b http://localhost:8081/haproxy | grep -c 'table'
echo "=== settings page ==="
curl -s -b /tmp/cjar_v2b http://localhost:8081/settings | grep -c 'panel_port'
echo "=== Missing features check ==="
grep -c 'batch_add\|killport\|flatpickr\|vanta\|three' /root/tcp-panel-v2/panel.py
echo "=== .gitignore missing ==="
ls /root/tcp-panel-v2/.gitignore 2>/dev/null && echo "has gitignore" || echo "NO gitignore"
