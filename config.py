"""Configuration - single source of truth for paths and settings"""
import os

# 一般不应对外暴露 / 易被扫描攻击的端口；面板自动分配本地中转口时必须避开。
# 含 SSH 及常见数据库/远程管理/文件共享/面板中间件/监控端口。
RESERVED_PORTS = frozenset({
    22, 2222,           # SSH（管理）
    21, 23,             # FTP / Telnet
    445, 139,           # SMB / NetBIOS
    3389, 5900, 5631,   # RDP / VNC / WinVNC
    3306, 5432, 6379, 27017,   # MySQL / PostgreSQL / Redis / MongoDB
    11211,              # memcached
    8080, 8081,         # 本面板等 Web 面板（自身用 8080）
    8000, 8008, 8888, 888,     # 常见 Web/管理面板
    5601, 9200, 9300,   # Kibana / Elasticsearch
    3000, 2375, 2376,   # Grafana / Docker
    9092, 15672,        # Kafka / RabbitMQ 管理
    161, 162,           # SNMP
    53, 323, 5355,      # DNS / chrony / LLMNR（系统本地）
    2049,               # NFS
})

class Config:
    def __init__(self):
        self.root = '/root/tcp-panel-v2'
        self.db_file = '/root/traffic.db'         # Same DB as v13 - no data disruption
        self.haproxy_sock = '/run/haproxy.sock'
        self.haproxy_cfg = '/etc/haproxy/haproxy.cfg'
        self.haproxy_pid = '/run/haproxy.pid'
        self.default_port = '8081'                 # Different port from v13 (8080)
        self.update_interval = 60                  # seconds between HAProxy stats updates
        self.check_timeout = 3                     # TCP check timeout in seconds
        self.check_max_workers = 30                # Concurrent check threads
        self.quota_check_interval = 60
