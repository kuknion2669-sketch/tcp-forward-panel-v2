#!/bin/bash
set -e

# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲
# TCP Forward Panel v14 鈥斺€?涓€閿畨瑁呰剼鏈?# 閫傜敤浜?Debian 11/12 (KVM)
# 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
err()   { echo -e "${RED}[ERR]${NC} $1"; exit 1; }

# 鈺斺晲鈺?Root check 鈺愨晲鈺愨晽
[[ $EUID -eq 0 ]] || err "璇蜂互 root 鐢ㄦ埛杩愯"

# 鈺斺晲鈺?OS check 鈺愨晲鈺愨晽
[[ -f /etc/debian_version ]] || err "浠呮敮鎸?Debian 鎿嶄綔绯荤粺"

# 鈺斺晲鈺?璇诲彇鍙橀噺 鈺愨晲鈺愨晽
ADMIN_USER="${ADMIN_USER:-admin}"
ADMIN_PASS="${ADMIN_PASS:-admin123}"
PANEL_PORT="${PANEL_PORT:-8080}"

# 鈺斺晲鈺?鏇存柊绯荤粺 鈺愨晲鈺愨晽
info "鏇存柊绯荤粺..."
apt update -y && apt upgrade -y

# 鈺斺晲鈺?瀹夎渚濊禆 鈺愨晲鈺愨晽
info "瀹夎渚濊禆锛歱ython3, pip, haproxy, socat, git..."
apt install -y python3 python3-pip haproxy socat git net-tools

# 妫€娴?Debian 鐗堟湰
DEBIAN_VERSION=$(cat /etc/debian_version | cut -d. -f1)
USE_VENV=false
[[ "$DEBIAN_VERSION" == "12" ]] && USE_VENV=true

if $USE_VENV; then
    info "妫€娴嬪埌 Debian 12锛屽皢浣跨敤 venv 闅旂 Python 鐜..."
    apt install -y python3-venv python3-full
    python3 -m venv /root/tcp-panel-v2/venv
    source /root/tcp-panel-v2/venv/bin/activate
    pip install flask
else
    info "瀹夎 Flask..."
    pip3 install flask
fi


# 鈺斺晲鈺?涓嬭浇椤圭洰 鈺愨晲鈺愨晽
info "涓嬭浇椤圭洰婧愪唬鐮?.."
cd /root
rm -rf tcp-panel-v2 2>/dev/null || true
git clone https://github.com/kuknion2669-sketch/tcp-forward-panel-v2.git tcp-panel-v2
cd tcp-panel-v2

# 鈺斺晲鈺?閰嶇疆 HAProxy systemd 鏈嶅姟 鈺愨晲鈺愨晽
info "閰嶇疆 HAProxy 绯荤粺鏈嶅姟..."
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

# 鈺斺晲鈺?绯荤粺浼樺寲 鈺愨晲鈺愨晽
info "绯荤粺浼樺寲..."
# BBR + TCP 鍔犻€?modprobe tcp_bbr 2>/dev/null || true
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

# 鈺斺晲鈺?鍒涘缓鏁版嵁搴擄紙鍐呭祵鑴氭湰锛夆晹鈺愨晲鈺?info "鍒濆鍖栨暟鎹簱..."
python3 -c "
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

# 鈺斺晲鈺?鍚姩闈㈡澘 鈺斺晲鈺愨晽
info "鍚姩闈㈡澘..."
cd /root/tcp-panel-v2
nohup /root/tcp-panel-v2/venv/bin/python panel.py > /root/panel-v2.log 2>&1 &
sleep 3

# 鈺斺晲鈺?娓呯悊 鈺斺晲鈺愨晽
rm -f /root/check_*.py /root/patch_*.py /root/fix_*.py /root/migrate_*.py /root/verify_*.py 2>/dev/null || true

# 鈺斺晲鈺?楠岃瘉 鈺斺晲鈺愨晽
PANEL_PID=$(netstat -tlnp 2>/dev/null | grep "$PANEL_PORT.*python" | grep -oP '\d+(?=/python\d*|/venv/bin/python)' || true)
if [[ -n "$PANEL_PID" ]]; then
  ok "闈㈡澘宸插惎鍔?(PID $PANEL_PID)"
  echo ""
  echo -e "  ${CYAN}闈㈡澘鍦板潃锛?{NC}  http://$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}'):$PANEL_PORT"
  echo -e "  ${CYAN}绠＄悊鍛樿处鍙凤細${NC}  $ADMIN_USER"
  echo -e "  ${CYAN}绠＄悊鍛樺瘑鐮侊細${NC}  $ADMIN_PASS"
  echo ""
  echo -e "  ${GREEN}瀹夎鎴愬姛锛?{NC}"
else
  err "闈㈡澘鍚姩澶辫触锛岃鏌ョ湅 /root/panel-v2.log"
fi
