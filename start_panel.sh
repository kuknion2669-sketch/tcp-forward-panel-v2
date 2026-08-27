#!/bin/sh
# Production launcher: runs the panel under gunicorn (single worker, 4 threads).
# The port is read from the panel DB (panel_port_v2).
cd /root/tcp-panel-v2 || exit 1
PYTHON="${PANEL_PYTHON:-python3}"
PORT=$("$PYTHON" - <<'PYEOF'
import sqlite3
try:
    con = sqlite3.connect('/root/traffic.db')
    row = con.execute("SELECT value FROM config WHERE key='panel_port_v2'").fetchone()
    print(row[0] if row and row[0].isdigit() else '8080')
    con.close()
except Exception:
    print('8080')
PYEOF
)
[ -n "$PORT" ] || PORT=8080
exec "$PYTHON" -m gunicorn -w 1 --threads 4 -b "0.0.0.0:$PORT" \
  --access-logfile - --error-logfile - panel:app
