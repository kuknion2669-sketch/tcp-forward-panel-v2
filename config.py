"""Configuration - single source of truth for paths and settings"""
import os

class Config:
    def __init__(self):
        self.root = '/root/tcp-panel-v2'
        self.db_file = '/root/traffic.db'         # Same DB as v13 - no data disruption
        self.haproxy_sock = '/run/haproxy/admin.sock'
        self.haproxy_cfg = '/etc/haproxy/haproxy.cfg'
        self.haproxy_pid = '/run/haproxy.pid'
        self.default_port = '8081'                 # Different port from v13 (8080)
        self.update_interval = 60                  # seconds between HAProxy stats updates
        self.check_timeout = 3                     # TCP check timeout in seconds
        self.check_max_workers = 30                # Concurrent check threads
        self.quota_check_interval = 60
