#!/bin/bash
# One-click upgrade: pull the latest code from GitHub, keep local fixes safe,
# update dependencies, then restart the panel.
set -e
cd /root/tcp-panel-v2
if [ -x venv/bin/python ]; then PY=venv/bin/python; else PY=python3; fi

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
"$PY" -m py_compile panel.py haproxy_ctl.py database.py check_mgr.py stats_collector.py config.py

echo "== 重启面板 =="
systemctl restart tcp-panel-v2 2>/dev/null || true
sleep 2
echo "升级完成：$(git log --oneline -1)"
