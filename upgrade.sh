#!/bin/bash
# One-click upgrade: pull the latest code from GitHub, keep local fixes safe,
# update dependencies, then restart the panel.
set -e
cd /root/tcp-panel-v2
if [ -x venv/bin/python ]; then
  PY="$(readlink -f venv/bin/python)"
else
  PY="$(command -v python3)"
fi

echo "== 检查更新 =="
git fetch origin 2>&1 | tail -2
BEHIND=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
if [ "$BEHIND" -eq 0 ]; then
  echo "已是最新版本 ($(git rev-parse --short HEAD))"
  exit 0
fi
echo "发现 $BEHIND 个新提交，开始升级..."

if git status --porcelain | grep -qv '^??'; then
  echo "暂存本地未提交修改..."
  git stash push -m "pre-upgrade-$(date +%Y%m%d_%H%M%S)"
fi

git rebase origin/main 2>/dev/null || git reset --hard origin/main

echo "== 更新依赖 =="
if [ -x venv/bin/pip ]; then
  venv/bin/pip install -q -r requirements.txt
else
  pip3 install -q -r requirements.txt
fi

echo "== 语法检查 =="
"$PY" -m py_compile panel.py panelworker.py reconcile.py haproxy_ctl.py \
  database.py check_mgr.py stats_collector.py config.py

echo "== 更新 systemd 服务 =="
chmod +x start_panel.sh start_worker.sh
mkdir -p /etc/systemd/system/tcp-panel-v2.service.d
cp tcp-panel-v2.service.d-bg-split.conf /etc/systemd/system/tcp-panel-v2.service.d/bg-split.conf
sed "s|__PYTHON_CMD__|$PY|g" tcp-panel-worker.service > /etc/systemd/system/tcp-panel-worker.service
cp tcp-panel-worker.timer /etc/systemd/system/tcp-panel-worker.timer
systemctl daemon-reload
systemctl enable tcp-panel-worker.timer

echo "== 重启面板与后台 worker =="
systemctl restart tcp-panel-v2 2>/dev/null || true
systemctl restart tcp-panel-worker.timer 2>/dev/null || true
systemctl start tcp-panel-worker.service
sleep 2
echo "升级完成：$(git log --oneline -1)"
