"""HAProxy control - socket management, config generation, reload, validation"""
import contextlib
import hashlib
import ipaddress
import socket
import subprocess
import os
import re
import logging
import sys
import tempfile
import time

from config import RESERVED_PORTS

try:
    import fcntl
except ImportError:  # pragma: no cover - tests run on Windows, production is Linux
    fcntl = None

log = logging.getLogger('haproxy')


def _valid_upstream_host(value):
    """Accept an IP address or a conservative DNS hostname, never raw config."""
    if not value or any(ch.isspace() for ch in value):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    if len(value) > 253:
        return False
    labels = value.rstrip('.').split('.')
    return all(
        1 <= len(label) <= 63
        and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?', label)
        for label in labels
    )


def _rule_endpoint(item):
    """Return validated ``(local_port, upstream_host:port)`` strings."""
    loc = str(item.get('local', '')).strip()
    host = str(item.get('ip', '')).strip()
    port = str(item.get('port', '')).strip()
    if not loc.isdigit() or not 1 <= int(loc) <= 65535:
        raise ValueError("invalid local port")
    if not _valid_upstream_host(host):
        raise ValueError("invalid upstream host")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("invalid upstream port")
    endpoint = f"[{host}]:{port}" if ':' in host else f"{host}:{port}"
    return loc, endpoint


def validate_rule_target(local, host, port):
    """Public validator used before a rule is written to SQLite."""
    return _rule_endpoint({'local': local, 'ip': host, 'port': port})


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
        # 2026-10-16: "在听端口全集"缓存（见 listening_ports / is_listening）
        self._listen_cache = None
        self._listen_ts = 0.0
        self.applied_hash_file = '/run/tcp-panel-applied.sha256'

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
        """Get per-backend bin/bout through the timeout-protected socket."""
        stats = {}
        for row in self.get_stats():
            pxname = row.get('pxname', '')
            if not pxname.startswith('be_'):
                continue
            port = pxname[3:]
            if not port.isdigit():
                continue
            try:
                stats[port] = {
                    'bin': int(row.get('bin', 0) or 0),
                    'bout': int(row.get('bout', 0) or 0),
                }
            except (TypeError, ValueError):
                pass
        return stats

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

    def server_status_map(self, force=False):
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
        if (not force and self._status_cache
                and now - self._status_cache_ts < 15):
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

    def is_listening(self, port, force=False):
        """Check if HAProxy is actually listening on a given port.

        2026-10-16: 改成查"一次取到的在听端口全集"（带 3 秒缓存），不再每个端口起一个
        `netstat -tlnp | grep` 子进程 —— 老写法单次实测 ~105ms（机器上 6000+ 连接），
        面板首页 198 条规则 = 20 秒、分组视图 40+ 秒、加节点时的 reload 校验 18.5 秒。
        """
        try:
            return int(port) in self.listening_ports(force=force)
        except Exception:
            return False

    def is_expired(self, expire_str):
        if not expire_str:
            return False
        try:
            return time.time() > float(expire_str)
        except Exception:
            return False

    def listening_ports(self, force=False, ttl=3.0):
        """Every TCP port currently in LISTEN state, on any local address.

        2026-10-16: 加 3 秒缓存（一次 ss/netstat 进程拿全集）。调用方要"改完立刻看结果"
        时传 force=True（reload 校验就这么用），避免拿到 reload 前的旧快照。
        """
        now = time.time()
        if (not force and self._listen_cache is not None
                and now - self._listen_ts < ttl):
            return set(self._listen_cache)
        ports = set()
        for cmd in ('ss -ltnH 2>/dev/null', 'netstat -tln 2>/dev/null'):
            out = subprocess.getoutput(cmd)
            if not out.strip():
                continue
            for line in out.splitlines():
                for tok in line.split():
                    if ':' in tok:
                        tail = tok.rsplit(':', 1)[-1]
                        if tail.isdigit():
                            ports.add(int(tail))
            break
        self._listen_cache = ports
        self._listen_ts = now
        return set(ports)

    def port_bindable(self, port):
        """True when HAProxy itself could bind this port on all interfaces."""
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(('0.0.0.0', port))
            return True
        except Exception:
            return False
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass

    def free_port(self, taken=None):
        """Pick a local port free in the DATABASE and free on the host.

        `taken` = local ports already owned by other rules (DB). Sockets alone
        are not enough: while HAProxy is down/restarting nothing is listening,
        so a port another rule already uses looks "free" and gets handed out
        twice. The generated config then binds the same port twice and HAProxy
        refuses to start (2026-10-15 customer-panel incident: two rules on
        local port 10000).
        """
        used = set(RESERVED_PORTS)
        for p in (taken or []):
            try:
                used.add(int(str(p).strip()))
            except (TypeError, ValueError):
                continue
        listening = self.listening_ports()
        for p in range(10000, 65535):
            if p in used or p in listening:
                continue
            if self.port_bindable(p):
                return str(p)
        log.error("free_port: no free local port in 10000-65534")
        return ''

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

    def validate_config(self, haproxy_cfg_str):
        """Validate with a unique temp file, safe under concurrent writers."""
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                    mode='w', suffix='.cfg', prefix='haproxy-validate-',
                    delete=False, encoding='utf-8') as f:
                tmp_path = f.name
                f.write(haproxy_cfg_str)
            r = subprocess.run(
                ['haproxy', '-c', '-f', tmp_path],
                capture_output=True, timeout=10,
            )
            try:
                if tmp_path:
                    os.unlink(tmp_path)
            except Exception:
                pass
            if r.returncode == 0:
                return True, "ok"
            return False, r.stderr.decode(errors='replace').strip()
        except Exception as e:
            try:
                if tmp_path:
                    os.unlink(tmp_path)
            except Exception:
                pass
            return False, str(e)

    @contextlib.contextmanager
    def _reload_lock(self):
        """Serialize compile+write+reload across all web workers and CLI jobs."""
        if fcntl is None:
            yield
            return
        with open('/run/tcp-panel-haproxy.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _write_config(self, text):
        """Atomically replace the HAProxy config so no reader sees a partial file."""
        path = self.config_file
        directory = os.path.dirname(path) or '.'
        fd, tmp_path = tempfile.mkstemp(
            prefix='.haproxy-config-', dir=directory, text=True
        )
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _config_hash(self, text):
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def applied_config_matches(self, text):
        """True only when this exact config was proven applied by a reload."""
        try:
            with open(self.applied_hash_file, encoding='ascii') as f:
                return f.read().strip() == self._config_hash(text)
        except OSError:
            return False

    def _mark_config_applied(self, text):
        path = self.applied_hash_file
        fd, tmp_path = tempfile.mkstemp(
            prefix='.tcp-panel-applied-', dir=os.path.dirname(path), text=True
        )
        try:
            with os.fdopen(fd, 'w', encoding='ascii') as f:
                f.write(self._config_hash(text) + '\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    def _reserve_frontend_ports(self, ports):
        """Prevent Linux ephemeral outbound sockets from stealing frontend ports."""
        if not sys.platform.startswith('linux'):
            return
        reserved = set()
        try:
            with open('/proc/sys/net/ipv4/ip_local_reserved_ports',
                      encoding='ascii') as f:
                current = f.read().strip()
            for part in current.split(','):
                if not part:
                    continue
                if '-' in part:
                    lo, hi = part.split('-', 1)
                    reserved.update(range(int(lo), int(hi) + 1))
                else:
                    reserved.add(int(part))
        except (OSError, ValueError):
            current = ''
        reserved.update(int(p) for p in ports if str(p).isdigit())
        value = ','.join(str(p) for p in sorted(reserved))
        try:
            r = subprocess.run(
                ['sysctl', '-w',
                 f'net.ipv4.ip_local_reserved_ports={value}'],
                capture_output=True, timeout=5,
            )
            if r.returncode != 0:
                log.warning("Could not reserve frontend ports: %s",
                            r.stderr.decode(errors='replace').strip())
            with open('/etc/sysctl.d/99-tcp-panel-reserved.conf',
                      'w', encoding='ascii') as f:
                f.write(f"net.ipv4.ip_local_reserved_ports = {value}\n")
        except Exception as e:  # noqa: BLE001
            log.warning("Could not reserve frontend ports: %s", e)

    def _haproxy_process_state(self):
        """Return ``(master_pid, {worker_pids})`` for runtime reload checks."""
        try:
            out = subprocess.run(
                ['systemctl', 'show', '-p', 'MainPID', '--value', 'haproxy'],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            master = int(out) if out.isdigit() else 0
        except Exception:
            master = 0
        workers = set()
        if master:
            try:
                out = subprocess.run(
                    ['pgrep', '-P', str(master), '-x', 'haproxy'],
                    capture_output=True, text=True, timeout=3,
                ).stdout
                workers = {int(p) for p in out.split() if p.isdigit()}
            except Exception:
                pass
        return master, workers

    def _runtime_applied(self, expected_ports, previous_state):
        """Wait until HAProxy has a new worker carrying every expected frontend."""
        previous_master, previous_workers = previous_state
        expected = {str(p) for p in expected_ports}
        deadline = time.time() + 12
        while time.time() < deadline:
            master, workers = self._haproxy_process_state()
            reloaded = (
                (master and master != previous_master)
                or bool(workers - previous_workers)
            )
            status = self.server_status_map(force=True)
            if reloaded and expected.issubset(status):
                new_workers = workers - previous_workers
                time.sleep(0.5)
                master_after, workers_after = self._haproxy_process_state()
                status_after = self.server_status_map(force=True)
                still_same_runtime = (
                    master_after == master
                    and (not new_workers or bool(new_workers & workers_after))
                )
                if still_same_runtime and expected.issubset(status_after):
                    return True
            time.sleep(0.25)
        return False

    def _restore_config(self, previous):
        if previous is None:
            try:
                os.unlink(self.config_file)
            except OSError:
                pass
            return
        self._write_config(previous)
        try:
            subprocess.run(
                ['systemctl', 'reload', 'haproxy'],
                capture_output=True, timeout=15,
            )
        except Exception as e:  # noqa: BLE001
            log.critical("Could not reload the previous HAProxy config: %s", e)

    def generate_config(self, data):
        """Build HAProxy config from database rules.

        Returns ``(config_text, expected_ports, duplicate_locals)``.  Keeping
        generation here means the synchronous reload path and the background
        reconciler always compare/write exactly the same configuration.
        """
        lines = [
            "global",
            # daemon removed - systemd manages in foreground
            "    maxconn 8192",
            f"    stats socket {self.sock} mode 600 level admin",
            "    hard-stop-after 1h",
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
        seen_locals = {}
        dup_locals = {}
        for item in data:
            if not item.get('enable', True):
                continue
            if self.is_expired(item.get('expire', '')):
                continue
            q = item.get('quota', 0)
            if q > 0 and item.get('used', 0) >= q * 1024:
                continue
            try:
                loc, endpoint = _rule_endpoint(item)
            except ValueError as e:
                raise ValueError(
                    f"rule {item.get('name', '')!r} ({item.get('local', '')}): {e}"
                ) from e
            if not loc:
                continue
            if loc in seen_locals:
                dup_locals.setdefault(loc, [seen_locals[loc]]).append(
                    item.get('name', '')
                )
            else:
                seen_locals[loc] = item.get('name', '')
            expected_ports.add(loc)
            lines.append(f"frontend fe_{loc}")
            lines.append(f"    bind 0.0.0.0:{loc}")
            lines.append("    mode tcp")
            lines.append(f"    default_backend be_{loc}")
            lines.append("")
            lines.append(f"backend be_{loc}")
            lines.append("    mode tcp")
            lines.append(
                f"    server s{loc} {endpoint} check inter 10s fall 3 rise 2"
            )
            lines.append("")

        return "\n".join(lines) + "\n", expected_ports, dup_locals

    def reload(self, data):
        """Validate, atomically write, reload, and prove the new worker is live."""
        with self._reload_lock():
            try:
                cfg, expected_ports, dup_locals = self.generate_config(data)
            except ValueError as e:
                log.error("Config generation rejected: %s", e)
                return False

            if dup_locals:
                log.critical(
                    "Duplicate local ports %s - config NOT written, HAProxy left "
                    "untouched. Fix the duplicate rule first.", dup_locals
                )
                return False

            valid, err = self.validate_config(cfg)
            if not valid:
                log.error("Config validation error: %s", err)
                return False

            self._reserve_frontend_ports(expected_ports)
            try:
                with open(self.config_file, encoding='utf-8') as f:
                    previous = f.read()
            except FileNotFoundError:
                previous = None

            previous_state = self._haproxy_process_state()
            self._write_config(cfg)
            try:
                r = subprocess.run(
                    ['systemctl', 'reload', 'haproxy'],
                    capture_output=True, timeout=15,
                )
            except Exception as e:  # noqa: BLE001
                log.error("HAProxy reload failed to start: %s", e)
                self._restore_config(previous)
                return False
            if r.returncode != 0:
                log.error(
                    "HAProxy reload failed (%s): %s", r.returncode,
                    r.stderr.decode(errors='replace').strip(),
                )
                self._restore_config(previous)
                return False

            if not self._runtime_applied(expected_ports, previous_state):
                log.error(
                    "HAProxy runtime did not adopt the new config within 12s; "
                    "restoring previous config"
                )
                self._restore_config(previous)
                return False
            self._mark_config_applied(cfg)

        log.info(
            f"HAProxy reloaded: "
            f"{len([d for d in data if d.get('enable', True)])} active rules"
        )
        return True
