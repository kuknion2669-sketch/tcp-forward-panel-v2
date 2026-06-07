"""HAProxy control - socket management, config generation, reload"""
import socket
import subprocess
import os
import re
import logging
import time

log = logging.getLogger('haproxy')

class HAProxyCtl:
    def __init__(self, config):
        self.cfg = config
        self.sock = config.haproxy_sock
        self.pid_file = config.haproxy_pid
        self.config_file = config.haproxy_cfg
        self._last_stats = {}
        self._last_update = 0.0
        self._bufsize = 65536

    def _sock_cmd(self, cmd):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(self.sock)
            s.sendall(cmd.encode() + b'\n')
            result = b''
            while True:
                chunk = s.recv(self._bufsize)
                if not chunk:
                    break
                result += chunk
            s.close()
            return result.decode()
        except Exception as e:
            log.warning(f"Sock cmd failed: {e}")
            return ''

    def get_stats(self):
        raw = self._sock_cmd('show stat')
        if not raw:
            return []
        lines = raw.strip().split('\n')
        if len(lines) < 2:
            return []
        hdrs = lines[0].split(',')
        result = []
        for line in lines[1:]:
            vals = line.split(',')
            if len(vals) >= 30 and vals[1] not in ('FRONTEND', 'BACKEND'):
                row = {h: v for h, v in zip(hdrs, vals) if h and v}
                result.append(row)
        return result

    def get_backend_stats(self):
        """Get per-backend bin/bout, returns dict of port -> {bin, bout}"""
        try:
            raw = subprocess.getoutput(f"echo 'show stat' | socat {self.sock} stdio 2>/dev/null")
            stats = {}
            for line in raw.strip().split('\n')[1:]:
                parts = line.split(',')
                if len(parts) < 10:
                    continue
                pxname = parts[0]
                svname = parts[1]
                if svname not in ('FRONTEND', 'BACKEND'):
                    continue
                if svname == 'FRONTEND':
                    continue
                port = pxname[3:] if pxname.startswith('be_') else ''
                if not port or not port.isdigit():
                    continue
                try:
                    stats[port] = {
                        'bin': int(parts[8] or 0),
                        'bout': int(parts[9] or 0)
                    }
                except Exception:
                    pass  # int() conversion failure, skip
            return stats
        except Exception as e:
            log.warning(f"get_backend_stats failed: {e}")
            return {}

    def get_api_stats(self):
        try:
            raw = self._sock_cmd('show stat')
            if not raw:
                return {'ok': False, 'error': 'socket not available'}
            lines = raw.strip().split('\n')
            if len(lines) < 2:
                return {'ok': False, 'error': 'no data'}
            hdrs = lines[0].split(',')
            result = []
            for line in lines[1:]:
                vals = line.split(',')
                if len(vals) >= 30 and vals[1] not in ('FRONTEND', 'BACKEND'):
                    row = {}
                    for i, h in enumerate(hdrs):
                        if i < len(vals):
                            row[h] = vals[i]
                    result.append(row)
            return {'ok': True, 'backends': result, 'total': len(result)}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def get_connections(self):
        try:
            raw = subprocess.getoutput('netstat -tn 2>/dev/null')
            result = {}
            for ln in raw.strip().split('\n'):
                p = ln.split()
                if len(p) >= 5 and p[0] not in ('Active', 'Proto'):
                    loc = p[3]
                    rem = p[4]
                    st = p[5] if len(p) > 5 else ''
                    i = loc.rfind(':')
                    if i >= 0:
                        po = loc[i+1:]
                        if po not in result:
                            result[po] = []
                        result[po].append({'remote': rem, 'state': st})
            return result
        except Exception as e:
            log.warning(f"get_connections failed: {e}")
            return {}

    def is_listening(self, port):
        try:
            return 'LISTEN' in subprocess.getoutput(f'netstat -tlnp 2>/dev/null | grep :{port}')
        except:
            return False

    def is_expired(self, expire_str):
        if not expire_str:
            return False
        try:
            return time.time() > float(expire_str)
        except:
            return False

    def free_port(self):
        for p in range(10000, 65535):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.05)
                if s.connect_ex(('127.0.0.1', p)) != 0:
                    s.close()
                    return str(p)
                s.close()
            except:
                continue
        return '10000'

    def parse_expire(self, expire_raw):
        if not expire_raw:
            from datetime import datetime, timedelta
            return str((datetime.now() + timedelta(days=30)).timestamp())
        try:
            t = expire_raw.replace('T', ' ')
            if t.isdigit():
                return str(int(t))
            from datetime import datetime
            for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
                try:
                    return str(datetime.strptime(t, fmt).timestamp())
                except Exception:
                    pass  # int() conversion failure, skip
        except Exception:
            pass
        from datetime import datetime, timedelta
        return str((datetime.now() + timedelta(days=30)).timestamp())

    def reload(self, data):
        """Generate HAProxy config and hot-reload"""
        lines = [
            "global",
            "    daemon",
            "    maxconn 4096",
            f"    stats socket {self.sock} mode 600 level admin",
            "    tune.bufsize 65536",
            "",
            "defaults",
            "    mode tcp",
            "    timeout connect 5000ms",
            "    timeout client 50000ms",
            "    timeout server 50000ms",
            "    option tcp-smart-connect",
            "    option tcp-smart-accept",
            "",
        ]
        for item in data:
            if not item.get('enable', True):
                continue
            if self.is_expired(item.get('expire', '')):
                continue
            q = item.get('quota', 0)
            if q > 0 and item.get('used', 0) >= q * 1024:
                continue
            loc = item.get('local', '')
            ip = item.get('ip', '')
            prt = item.get('port', '')
            if not loc or not ip or not prt:
                continue
            lines.append(f"frontend fe_{loc}")
            lines.append(f"    bind 0.0.0.0:{loc}")
            lines.append("    mode tcp")
            lines.append(f"    default_backend be_{loc}")
            lines.append("")
            lines.append(f"backend be_{loc}")
            lines.append("    mode tcp")
            lines.append(f"    server s{loc} {ip}:{prt} check inter 10s fall 3 rise 2")
            lines.append("")

        cfg = '\n'.join(lines) + '\n'
        with open(self.config_file, 'w') as f:
            f.write(cfg)

        # Hot reload via systemctl
        try:
            subprocess.run('systemctl restart haproxy', shell=True, capture_output=True, timeout=10)
        except Exception as e:
            log.error(f"Reload failed: {e}")

        # Disable exhausted nodes
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(self.sock)
            for it in data:
                if not it.get('enable', True):
                    continue
                lo = it.get('local', '')
                if not lo:
                    continue
                if self.is_expired(it.get('expire', '')) or \
                   (it.get('quota', 0) > 0 and it.get('used', 0) >= it.get('quota', 0) * 1024):
                    s.sendall(f"shutdown sessions server be_{lo}/s{lo}\n".encode())
            s.close()
        except Exception:
            pass

        log.info(f"HAProxy reloaded: {len([d for d in data if d.get('enable', True)])} active rules")
