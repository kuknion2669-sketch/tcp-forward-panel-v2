#!/bin/bash
set -e

# ═══════════════════════════════════════════════════
# TCP Forward Panel v14 — 一键安装脚本
# 适用于 Debian 11/12 (KVM)
# ═══════════════════════════════════════════════════

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC} $1"; exit 1; }

# ── Root check ──
[[ $EUID -eq 0 ]] || err "请以 root 用户运行"

# ── OS check ──
[[ -f /etc/debian_version ]] || err "仅支持 Debian 系列系统"

# ── 获取参数 ──
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-admin123}"
PANEL_PORT="${PANEL_PORT:-8080}"

# ── 更新系统 ──
info "更新系统..."
apt update -y && apt upgrade -y

# ── 安装依赖 ──
info "安装依赖包: python3, pip, haproxy, socat, git..."
apt install -y python3 python3-pip haproxy socat git net-tools

# ── 克隆仓库 ──
info "克隆面板代码..."
cd /root
rm -rf tcp-panel-v2 2>/dev/null || true
git clone https://github.com/kuknion2669-sketch/tcp-forward-panel-v2.git tcp-panel-v2
cd tcp-panel-v2

# ── 检测 Debian 版本，确定 Python 环境 ──
DEBIAN_VERSION=$(cat /etc/debian_version | cut -d. -f1)
PYTHON_CMD="python3"

if [[ "$DEBIAN_VERSION" == "12" ]]; then
    info "检测到 Debian 12，使用虚拟环境安装 Python 包..."
    apt install -y python3-venv python3-full
    python3 -m venv /root/tcp-panel-v2/venv
    source /root/tcp-panel-v2/venv/bin/activate
    pip install flask
    PYTHON_CMD="/root/tcp-panel-v2/venv/bin/python"
else
    info "安装 Flask..."
    pip3 install flask
fi

# ── 配置 HAProxy systemd 服务 ──
info "配置 HAProxy 服务..."
cat > /lib/systemd/system/haproxy.service << 'SERVICE'
[Unit]
Description=HAProxy Load Balancer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment="CONFIG=/etc/haproxy/haproxy.cfg" "PIDFILE=/run/haproxy.pid"
ExecStartPre=/usr/sbin/haproxy -c -f $CONFIG -q
ExecStart=/usr/sbin/haproxy -f $CONFIG -p $PIDFILE
ExecReload=/usr/sbin/haproxy -c -f $CONFIG -q
ExecReload=/usr/sbin/haproxy -f $CONFIG -p $PIDFILE -sf $(cat $PIDFILE 2>/dev/null)
Restart=always
RestartSec=5
LimitNOFILE=65535
KillMode=process
SuccessExitStatus=143

[Install]
WantedBy=multi-user.target
SERVICE

systemctl daemon-reload
systemctl enable haproxy

# ── 系统调优 ──
info "系统调优..."
modprobe tcp_bbr 2>/dev/null || true
if ! grep -q 'tcp_bbr' /etc/modules-load.d/modules.conf 2>/dev/null; then
  echo tcp_bbr >> /etc/modules-load.d/modules.conf 2>/dev/null || true
fi
cat >> /etc/sysctl.d/99-tcp-forward.conf << 'SYSCTL' || true
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_fastopen = 3
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.ip_local_port_range = 1024 65535
SYSCTL
sysctl -p /etc/sysctl.d/99-tcp-forward.conf 2>/dev/null || true

# ── 创建数据库（空表结构）──
info "初始化数据库..."
$PYTHON_CMD -c "
import sqlite3, json, hashlib
db = sqlite3.connect('/root/traffic.db')
c = db.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, local TEXT NOT NULL, ip TEXT NOT NULL,
    port TEXT NOT NULL, expire TEXT DEFAULT \"\",
    quota REAL DEFAULT 0, used REAL DEFAULT 0,
    used_in REAL DEFAULT 0, used_out REAL DEFAULT 0,
    enable INTEGER DEFAULT 1, note TEXT DEFAULT \"\"
)''')
c.execute('''CREATE TABLE IF NOT EXISTS traffic (
    date TEXT, port TEXT, name TEXT, used REAL,
    online INT DEFAULT 0, quota REAL DEFAULT 0,
    PRIMARY KEY (date, port)
)''')
c.execute('''CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    time TEXT, port TEXT, name TEXT,
    event_type TEXT, message TEXT
)''')
c.execute('''CREATE TABLE IF NOT EXISTS daily (
    date TEXT PRIMARY KEY,
    total_traffic REAL DEFAULT 0, total_in REAL DEFAULT 0,
    total_out REAL DEFAULT 0, online_count INT DEFAULT 0,
    total_nodes INT DEFAULT 0
)''')
c.execute('''CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY, value TEXT
)''')
h = hashlib.sha256('$ADMIN_PASS'.encode()).hexdigest()
c.execute('INSERT OR REPLACE INTO config VALUES (?,?)', ('panel_port_v2', '$PANEL_PORT'))
c.execute('INSERT OR REPLACE INTO config VALUES (?,?)', ('username', '$ADMIN_USER'))
c.execute('INSERT OR REPLACE INTO config VALUES (?,?)', ('password_hash', h))
db.commit()
db.close()
"

# ── 启动面板 ──
info "启动面板..."
cd /root/tcp-panel-v2
nohup $PYTHON_CMD panel.py > /root/panel-v2.log 2>&1 &
sleep 3

# ── 清理 ──
rm -f /root/check_*.py /root/patch_*.py /root/fix_*.py /root/migrate_*.py /root/verify_*.py 2>/dev/null || true

# ── 验证 ──
PANEL_PID=$(netstat -tlnp 2>/dev/null | grep "$PANEL_PORT.*python" | grep -oP '\d+(?=/python\w*|/venv/bin/python)' || true)
if [[ -n "$PANEL_PID" ]]; then
  ok "面板已启动 (PID $PANEL_PID)"
  echo ""
  echo -e "  ${CYAN}面板地址:${NC}  http://$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'):$PANEL_PORT"
  echo -e "  ${CYAN}登录账号:${NC}  $ADMIN_USER"
  echo -e "  ${CYAN}登录密码:${NC}  $ADMIN_PASS"
  echo ""
  echo -e "  ${GREEN}安装完成！${NC}"
else
  err "面板启动失败，请检查 /root/panel-v2.log"
fi
