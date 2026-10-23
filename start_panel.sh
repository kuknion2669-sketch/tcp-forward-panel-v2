#!/bin/sh
# Production launcher: runs the panel under gunicorn.
# The port is read from the panel DB (panel_port_v2).
#
# 2026-10-16: worker 从 1 个提到 3 个。
#   以前只能开 1 个，是因为"流量统计 / 自动停用"那段后台活跑在 Web 进程里，
#   多开几个就会各记一份、流量翻倍 —— 现在后台活已经拆到
#   tcp-panel-worker.service（见 panelworker.py / start_worker.sh），
#   且 Web 侧设了 PANEL_BG_DISABLED=1（见 tcp-panel-v2.service.d/bg-split.conf），
#   所以多 worker 是安全的。
cd /root/tcp-panel-v2 || exit 1
export PANEL_BG_DISABLED=1
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
exec "$PYTHON" -m gunicorn -w 3 --threads 4 -b "0.0.0.0:$PORT" \
  --access-logfile - --error-logfile - panel:app
