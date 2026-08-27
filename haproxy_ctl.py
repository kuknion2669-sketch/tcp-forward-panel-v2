"""HAProxy control - socket management, config generation, reload, validation"""
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
        self._status_cache = {}
        self._status_cache_ts = 0.0

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
        hdrs = [h.strip().lstrip('#').strip() for h in lines[0].split(',')]
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
            raw = subprocess.getoutput(
                f"echo 'show stat' | socat {self.sock} stdio 2>/dev/null"
            )
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
                    pass
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

    def server_status_map(self):
        """Return {local_port: bool} using HAProxy's authoritative status.

        A node is treated as ONLINE only when HAProxy reports its per-server
        row status as 'UP' (e.g. 'UP', 'UP 1/3') from `show stat`. This is the
        same health signal HAProxy uses to route real traffic, so the panel's
        online/offline badge matches what is actually being forwarded.
        FRONTEND/BACKEND aggregate rows are skipped; we read the server row.

        Result is cached for a short window so repeated dashboard renders do
        not hammer the stats socket on every request.
        """
        now = time.time()
        if self._status_cache and now - self._status_cache_ts < 15:
            return self._status_cache

        raw = self._sock_cmd('show stat')
        result = {}
        if raw:
            lines = raw.strip().split('\n')
            if len(lines) >= 2:
                hdr = [h.strip().lstrip('#').strip() for h in lines[0].split(',')]
                try:
                    status_idx = hdr.index('status')
                except ValueError:
                    status_idx = 17
                for line in lines[1:]:
                    parts = line.split(',')
                    if len(parts) <= status_idx:
                        continue
                    pxname = parts[0]
                    svname = parts[1]
                    if svname in ('FRONTEND', 'BACKEND'):
                        continue
                    if not pxname.startswith('be_'):
                        continue
                    port = pxname[3:]
                    if not port.isdigit():
                        continue
                    result[port] = parts[status_idx].startswith('UP')

        self._status_cache = result
        self._status_cache_ts = now
        return result

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
                        po = loc[i + 1:]
                        if po not in result:
                            result[po] = []
                        result[po].append({'remote': rem, 'state': st})
            return result
        except Exception as e:
            log.warning(f"get_connections failed: {e}")
            return {}

    def is_listening(self, port):
        """Check if HAProxy is actually listening on a given port"""
        try:
            return 'LISTEN' in subprocess.getoutput(
                f'netstat -tlnp 2>/dev/null | grep :{port}'
            )
        except Exception:
            return False

    def is_expired(self, expire_str):
        if not expire_str:
            return False
        try:
            return time.time() > float(expire_str)
        except Exception:
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
            except Exception:
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
            for fmt in (
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%d %H:%M',
                '%Y-%m-%d',
            ):
                try:
                    return str(datetime.strptime(t, fmt).timestamp())
                except Exception:
                    pass
        except Exception:
            pass
        from datetime import datetime, timedelta
        return str((datetime.now() + timedelta(days=30)).timestamp())

    def validate_config(self, haproxy_cfg_str, tmp_path="/tmp/haproxy_validate.cfg"):
        """Write config to a fixed temp path, then run haproxy -c to validate.
        Uses a fixed path to avoid tempfile/subprocess edge cases."""
        import os
        try:
            with open(tmp_path, 'w') as f:
                f.write(haproxy_cfg_str)
            r = subprocess.run(
                f'haproxy -c -f {tmp_path}',
                shell=True, capture_output=True, timeout=10
            )
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            if r.returncode == 0:
                return True, "ok"
            return False, r.stderr.decode().strip()
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return False, str(e)


    def reload(self, data):
        """Generate config, validate, reload via haproxy -sf, verify listening"""
        lines = [
            "global",
            # daemon removed - systemd manages in foreground
            "    maxconn 8192",
            f"    stats socket {self.sock} mode 600 level admin",
            "    tune.bufsize 65536",
            "",
            "defaults",
            "    mode tcp",
            "    timeout connect 5000ms",
            "    timeout client 1h",
            "    timeout server 1h",
            "",
        ]

        expected_ports = set()
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
            expected_ports.add(loc)
            lines.append(f"frontend fe_{loc}")
            lines.append(f"    bind 0.0.0.0:{loc}")
            lines.append("    mode tcp")
            lines.append(f"    default_backend be_{loc}")
            lines.append("")
            lines.append(f"backend be_{loc}")
            lines.append("    mode tcp")
            lines.append(
                f"    server s{loc} {ip}:{prt} check inter 10s fall 3 rise 2"
            )
            lines.append("")

        # Validate config before writing
        cfg = '\n'.join(lines) + '\n'
        valid, err = self.validate_config(cfg)
        if not valid:
            log.error(f"Config validation error: {err}")
            return False

        # Write valid config
        with open(self.config_file, 'w') as f:
            f.write(cfg)

        # Reload via haproxy -sf, NON-BLOCKING so the panel never blocks
        # 15s and never leaves foreground/orphaned processes that time out.
        # The old master is signalled to drain (-sf); haproxy-watchdog force-
        # cleans drained/stale processes older than the grace window, so they
        # can never accumulate (this host previously reached 42 processes).
        try:
            pid_raw = subprocess.getoutput(
                'systemctl show -p MainPID --value haproxy 2>/dev/null'
            ).strip()
            if not pid_raw.isdigit():
                pid_raw = subprocess.getoutput(
                    'pgrep -x haproxy | sort -n | tail -1'
                ).strip()
            pid = pid_raw.split()[0] if pid_raw else ''
            if pid and pid.isdigit():
                import os, signal
                os.kill(int(pid), signal.SIGUSR2)
                log.info(f"HAProxy config reloaded via USR2 (pid {pid})")
            else:
                log.warning(
                    f"Invalid PID '{pid_raw}', falling back to restart"
                )
                subprocess.Popen(
                    'systemctl restart haproxy >/dev/null 2>&1',
                    shell=True, start_new_session=True,
                )
        except Exception as e:
            log.error(f"Reload failed: {e}")
            subprocess.Popen(
                'systemctl restart haproxy >/dev/null 2>&1',
                shell=True, start_new_session=True,
            )

        # Wait for HAProxy to settle
        time.sleep(2)

        # Validate: check every expected port is actually listening
        missing_ports = []
        for port in sorted(expected_ports):
            if not self.is_listening(port):
                missing_ports.append(port)

        if missing_ports:
            log.error(
                f"Reload validation FAILED: ports not listening: "
                f"{missing_ports}"
            )
            # Try one more restart
            subprocess.run(
                'systemctl restart haproxy',
                shell=True, capture_output=True, timeout=15
            )
            time.sleep(2)
            still_missing = [
                p for p in missing_ports if not self.is_listening(p)
            ]
            if still_missing:
                log.critical(
                    f"Ports still missing after restart: {still_missing}. "
                    f"Config may have duplicates or conflicts."
                )
                return False

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
                   (it.get('quota', 0) > 0 and
                    it.get('used', 0) >= it.get('quota', 0) * 1024):
                    s.sendall(
                        f"shutdown sessions server be_{lo}/s{lo}\n".encode()
                    )
            s.close()
        except Exception:
            pass

        log.info(
            f"HAProxy reloaded: "
            f"{len([d for d in data if d.get('enable', True)])} active rules"
        )
        return True
