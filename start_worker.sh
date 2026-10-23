#!/bin/sh
set -eu

cd /root/tcp-panel-v2 || exit 1
PYTHON="${PANEL_PYTHON:-/root/tcp-panel-v2/venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

exec "$PYTHON" panelworker.py "$@"
