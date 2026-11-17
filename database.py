"""Database operations - all SQLite access centralized here"""
import contextlib
import os
import sqlite3
import hashlib
import logging
from datetime import datetime

try:
    import fcntl
except ImportError:  # pragma: no cover - production is Linux
    fcntl = None

log = logging.getLogger('db')

class Database:
    def __init__(self, db_file):
        self.db_file = db_file
        self._init_db()

    def _connect(self):
        """Open a WAL connection with busy timeout to avoid SQLITE_BUSY."""
        conn = sqlite3.connect(self.db_file, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception as e:
            log.warning(f"DB PRAGMA failed: {e}")
        return conn

    @contextlib.contextmanager
    def locked(self):
        """Serialize read-modify-write operations across web and worker processes."""
        if fcntl is None:
            yield
            return
        path = ('/run/tcp-panel-db.lock'
                if os.path.abspath(self.db_file) == '/root/traffic.db'
                else self.db_file + '.lock')
        with open(path, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _init_db(self):
        conn = self._connect()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, local TEXT NOT NULL, ip TEXT NOT NULL,
            port TEXT NOT NULL, expire TEXT DEFAULT "",
            quota REAL DEFAULT 0, used REAL DEFAULT 0,
            used_in REAL DEFAULT 0, used_out REAL DEFAULT 0,
            enable INTEGER DEFAULT 1, note TEXT DEFAULT "",
            group_name TEXT DEFAULT ""
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
        c.execute('''CREATE TABLE IF NOT EXISTS traffic_cursor (
            port TEXT PRIMARY KEY,
            bin_bytes INTEGER DEFAULT 0,
            bout_bytes INTEGER DEFAULT 0,
            observed_at TEXT DEFAULT ''
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS node_daily (
            date TEXT NOT NULL,
            port TEXT NOT NULL,
            name TEXT DEFAULT '',
            in_mb REAL DEFAULT 0,
            out_mb REAL DEFAULT 0,
            first_seen TEXT DEFAULT '',
            last_seen TEXT DEFAULT '',
            PRIMARY KEY (date, port)
        )''')
        c.execute(
            'CREATE INDEX IF NOT EXISTS idx_node_daily_port_date '
            'ON node_daily(port, date)'
        )
        c.execute('''CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY, value TEXT
        )''')
        for k, v in [('panel_port', '8081'), ('username', 'admin'),
                     ('password_hash', '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9')]:
            c.execute('INSERT OR IGNORE INTO config VALUES (?,?)', (k, v))
        # Migration: add group_name for v13->v2 upgrade
        try:
            cols = [r[1] for r in c.execute("PRAGMA table_info(rules)").fetchall()]
            if "group_name" not in cols:
                c.execute("ALTER TABLE rules ADD COLUMN group_name TEXT DEFAULT ''")
                log.info("Migration: added group_name column")
        except Exception as e:
            log.warning(f"Migration: could not add group_name - {e}")
        try:
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_rules_local_active_unique "
                "ON rules(local) WHERE enable=1"
            )
        except sqlite3.IntegrityError as e:
            log.critical("Duplicate local ports prevent the unique index: %s", e)
        conn.commit()
        conn.close()

    def load(self):
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM rules ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def save(self, data):
        conn = None
        try:
            conn = self._connect()
            c = conn.cursor()
            c.execute("DELETE FROM rules")
            for item in data:
                c.execute('''INSERT INTO rules (name, local, ip, port, expire, quota, used, used_in, used_out, enable, note, group_name)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (item.get('name', ''), item.get('local', ''), item.get('ip', ''),
                     item.get('port', ''), item.get('expire', ''), item.get('quota', 0),
                     item.get('used', 0), item.get('used_in', 0), item.get('used_out', 0),
                     1 if item.get('enable', True) else 0, item.get('note', ''), item.get('group_name', '')))
            conn.commit()
            return True
        except Exception as e:
            log.error(f"Save failed: {e}")
            return False
        finally:
            if conn is not None:
                conn.close()

    def save_usage(self, data):
        """Update only traffic counters, never overwrite concurrent rule edits."""
        try:
            conn = self._connect()
            c = conn.cursor()
            for item in data:
                where = "id=?" if item.get("id") is not None else "local=?"
                key = item.get("id", item.get("local", ""))
                c.execute(
                    "UPDATE rules SET used=?, used_in=?, used_out=? WHERE " + where,
                    (item.get("used", 0), item.get("used_in", 0),
                     item.get("used_out", 0), key),
                )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            log.error("save_usage failed: %s", e)
            return False

    def reset_usage(self):
        try:
            conn = self._connect()
            conn.execute(
                "UPDATE rules SET used=0, used_in=0, used_out=0"
            )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            log.error("reset_usage failed: %s", e)
            return False

    def apply_traffic_snapshot(self, samples, observed_at=None):
        """Convert HAProxy cumulative byte counters into per-node daily deltas.

        The first observation for a port is a baseline and does not add usage.
        If HAProxy reloaded and its counters moved backwards, the current value
        is treated as the usage accumulated in the new counter epoch.
        """
        observed_at = observed_at or datetime.now().strftime(
            '%Y-%m-%d %H:%M:%S.%f'
        )
        date = observed_at[:10]
        display_time = observed_at[:19]
        conn = None
        result = {'ports': 0, 'in_mb': 0.0, 'out_mb': 0.0, 'resets': 0}
        try:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rules = {
                str(r['local']): dict(r)
                for r in cur.execute('SELECT id,name,local FROM rules')
                if r['local'] is not None
            }
            cursors = {
                str(r['port']): dict(r)
                for r in cur.execute('SELECT * FROM traffic_cursor')
            }

            for port, counters in samples.items():
                port = str(port)
                if port not in rules:
                    continue
                try:
                    bin_bytes = max(0, int(counters.get('bin', 0) or 0))
                    bout_bytes = max(0, int(counters.get('bout', 0) or 0))
                except (TypeError, ValueError):
                    continue

                prev = cursors.get(port)
                delta_in = delta_out = 0
                if prev is not None:
                    prev_observed = str(prev.get('observed_at') or '')
                    if prev_observed and observed_at <= prev_observed:
                        continue
                    prev_bin = max(0, int(prev.get('bin_bytes', 0) or 0))
                    prev_bout = max(0, int(prev.get('bout_bytes', 0) or 0))
                    if bin_bytes >= prev_bin:
                        delta_in = bin_bytes - prev_bin
                    else:
                        delta_in = bin_bytes
                        result['resets'] += 1
                    if bout_bytes >= prev_bout:
                        delta_out = bout_bytes - prev_bout
                    else:
                        delta_out = bout_bytes
                        result['resets'] += 1

                cur.execute(
                    '''INSERT INTO traffic_cursor
                       (port, bin_bytes, bout_bytes, observed_at)
                       VALUES (?,?,?,?)
                       ON CONFLICT(port) DO UPDATE SET
                         bin_bytes=excluded.bin_bytes,
                         bout_bytes=excluded.bout_bytes,
                         observed_at=excluded.observed_at''',
                    (port, bin_bytes, bout_bytes, observed_at),
                )

                delta_in_mb = delta_in / 1048576.0
                delta_out_mb = delta_out / 1048576.0
                if delta_in_mb <= 0 and delta_out_mb <= 0:
                    continue

                rule = rules[port]
                name = rule.get('name') or ''
                cur.execute(
                    '''UPDATE rules SET
                         used_in=ROUND(COALESCE(used_in,0)+?,3),
                         used_out=ROUND(COALESCE(used_out,0)+?,3),
                         used=ROUND(COALESCE(used,0)+?,3)
                       WHERE local=?''',
                    (delta_in_mb, delta_out_mb,
                     delta_in_mb + delta_out_mb, port),
                )
                cur.execute(
                    '''INSERT INTO node_daily
                       (date, port, name, in_mb, out_mb, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(date, port) DO UPDATE SET
                         name=excluded.name,
                         in_mb=node_daily.in_mb+excluded.in_mb,
                         out_mb=node_daily.out_mb+excluded.out_mb,
                         last_seen=excluded.last_seen''',
                    (date, port, name, delta_in_mb, delta_out_mb,
                     display_time, display_time),
                )
                result['ports'] += 1
                result['in_mb'] += delta_in_mb
                result['out_mb'] += delta_out_mb

            conn.commit()
            result['in_mb'] = round(result['in_mb'], 3)
            result['out_mb'] = round(result['out_mb'], 3)
            return result
        except Exception as e:
            if conn is not None:
                conn.rollback()
            log.error("apply_traffic_snapshot failed: %s", e)
            return None
        finally:
            if conn is not None:
                conn.close()

    def clear_traffic_cursor(self, port):
        try:
            conn = self._connect()
            conn.execute('DELETE FROM traffic_cursor WHERE port=?', (str(port),))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            log.error("clear_traffic_cursor failed: %s", e)
            return False

    def reset_traffic_stats(self, samples, observed_at=None):
        """Start a clean traffic epoch from the current HAProxy counters."""
        now = observed_at or datetime.now().strftime(
            '%Y-%m-%d %H:%M:%S.%f'
        )
        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute('DELETE FROM traffic_cursor')
            cur.execute('DELETE FROM node_daily')
            cur.execute('UPDATE rules SET used=0, used_in=0, used_out=0')
            for port, counters in samples.items():
                try:
                    bin_bytes = max(0, int(counters.get('bin', 0) or 0))
                    bout_bytes = max(0, int(counters.get('bout', 0) or 0))
                except (TypeError, ValueError):
                    continue
                cur.execute(
                    '''INSERT OR REPLACE INTO traffic_cursor
                       (port, bin_bytes, bout_bytes, observed_at)
                       VALUES (?,?,?,?)''',
                    (str(port), bin_bytes, bout_bytes, now),
                )
            cur.execute(
                'INSERT OR REPLACE INTO config VALUES (?,?)',
                ('traffic_stats_epoch', now),
            )
            cur.execute(
                'INSERT OR REPLACE INTO config VALUES (?,?)',
                ('traffic_stats_version', '2'),
            )
            conn.commit()
            return now
        except Exception as e:
            if conn is not None:
                conn.rollback()
            log.error("reset_traffic_stats failed: %s", e)
            return None
        finally:
            if conn is not None:
                conn.close()

    def get_traffic_daily_totals(self, days=30):
        try:
            conn = self._connect()
            cur = conn.cursor()
            rows = list(cur.execute(
                '''SELECT date,
                          ROUND(SUM(in_mb),3),
                          ROUND(SUM(out_mb),3)
                   FROM node_daily
                   GROUP BY date
                   ORDER BY date DESC
                   LIMIT ?''',
                (int(days),),
            ))
            conn.close()
            return list(reversed(rows))
        except Exception as e:
            log.error("get_traffic_daily_totals failed: %s", e)
            return []

    def get_traffic_nodes_for_date(self, date):
        try:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = [
                dict(r) for r in cur.execute(
                    '''SELECT date,port,name,in_mb,out_mb,first_seen,last_seen
                       FROM node_daily
                       WHERE date=?
                       ORDER BY (in_mb+out_mb) DESC, port''',
                    (date,),
                )
            ]
            conn.close()
            return rows
        except Exception as e:
            log.error("get_traffic_nodes_for_date failed: %s", e)
            return []

    def get_traffic_totals_by_node(self, start_date, end_date):
        try:
            conn = self._connect()
            cur = conn.cursor()
            rows = list(cur.execute(
                '''SELECT port, name,
                          ROUND(SUM(in_mb),3),
                          ROUND(SUM(out_mb),3)
                   FROM node_daily
                   WHERE date BETWEEN ? AND ?
                   GROUP BY port
                   ORDER BY SUM(in_mb+out_mb) DESC, port''',
                (start_date, end_date),
            ))
            conn.close()
            return rows
        except Exception as e:
            log.error("get_traffic_totals_by_node failed: %s", e)
            return []

    def export_traffic_daily(self, start_date=None, end_date=None):
        try:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            sql = '''SELECT date,port,name,in_mb,out_mb,
                            ROUND(in_mb+out_mb,3) AS total_mb,
                            first_seen,last_seen
                     FROM node_daily'''
            args = []
            if start_date and end_date:
                sql += ' WHERE date BETWEEN ? AND ?'
                args = [start_date, end_date]
            sql += ' ORDER BY date, port'
            rows = [dict(r) for r in cur.execute(sql, args)]
            conn.close()
            return rows
        except Exception as e:
            log.error("export_traffic_daily failed: %s", e)
            return []

    def set_enable(self, local, enable):
        try:
            conn = self._connect()
            conn.execute("UPDATE rules SET enable=? WHERE local=?", (1 if enable else 0, local))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            log.error(f"Set enable failed: {e}")
            return False

    def log_event(self, port, name, event_type, message=''):
        try:
            conn = self._connect()
            cur = conn.cursor()
            from datetime import datetime
            cur.execute('INSERT INTO events (time, port, name, event_type, message) VALUES (?,?,?,?,?)',
                        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), port, name, event_type, message))
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"Log event failed: {e}")

    def get_config(self):
        conn = self._connect()
        c = conn.cursor()
        c.execute('SELECT key, value FROM config')
        rows = dict(c.fetchall())
        conn.close()
        return rows

    def set_config(self, key, value):
        try:
            conn = self._connect()
            conn.execute('INSERT OR REPLACE INTO config VALUES (?,?)', (key, value))
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"Set config failed: {e}")

    def check_auth(self, user, pwd):
        cfg = self.get_config()
        h = hashlib.sha256(pwd.encode()).hexdigest()
        return user == cfg.get('username') and h == cfg.get('password_hash')

    def get_daily(self, days=30):
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute('SELECT date, total_traffic, total_in, total_out FROM daily ORDER BY date DESC LIMIT ?', (days,))
            rows = list(reversed(cur.fetchall()))
            conn.close()
            return rows
        except Exception as e:
            log.error(f"Get daily failed: {e}")
            return []

    
    def get_events(self, limit=100, offset=0):
        try:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute('SELECT * FROM events ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset))
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            log.error(f"Get events failed: {e}")
            return []

    def save_daily(self, date, total, total_in, total_out, online, total_nodes):

        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute('SELECT total_traffic, total_in, total_out FROM daily ORDER BY date DESC LIMIT 1')
            prev = cur.fetchone()
            if prev and (total < prev[0] or total_in < prev[1] or total_out < prev[2]):
                log.warning(f"Daily cumulative went backwards: prev_total={prev[0]}, cur_total={total}")
            new_total, new_in, new_out = total, total_in, total_out
            cur.execute('INSERT OR REPLACE INTO daily (date, total_traffic, total_in, total_out, online_count, total_nodes) VALUES (?,?,?,?,?,?)',
                        (date, new_total, new_in, new_out, online, total_nodes))
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"Save daily failed: {e}")
