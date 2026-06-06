"""Database operations - all SQLite access centralized here"""
import sqlite3
import hashlib
import logging

log = logging.getLogger('db')

class Database:
    def __init__(self, db_file):
        self.db_file = db_file
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, local TEXT NOT NULL, ip TEXT NOT NULL,
            port TEXT NOT NULL, expire TEXT DEFAULT "",
            quota REAL DEFAULT 0, used REAL DEFAULT 0,
            used_in REAL DEFAULT 0, used_out REAL DEFAULT 0,
            enable INTEGER DEFAULT 1, note TEXT DEFAULT ""
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
        for k, v in [('panel_port', '8081'), ('username', 'admin'),
                     ('password_hash', '240be518fabd2724ddb6f04eeb1da5967448d7e831c08c8fa822809f74c720a9')]:
            c.execute('INSERT OR IGNORE INTO config VALUES (?,?)', (k, v))
        conn.commit()
        conn.close()

    def load(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM rules ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def save(self, data):
        try:
            conn = sqlite3.connect(self.db_file)
            c = conn.cursor()
            c.execute("DELETE FROM rules")
            for item in data:
                c.execute('''INSERT INTO rules (name, local, ip, port, expire, quota, used, used_in, used_out, enable, note)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (item.get('name', ''), item.get('local', ''), item.get('ip', ''),
                     item.get('port', ''), item.get('expire', ''), item.get('quota', 0),
                     item.get('used', 0), item.get('used_in', 0), item.get('used_out', 0),
                     1 if item.get('enable', True) else 0, item.get('note', '')))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            log.error(f"Save failed: {e}")
            return False

    def log_event(self, port, name, event_type, message=''):
        try:
            conn = sqlite3.connect(self.db_file)
            cur = conn.cursor()
            from datetime import datetime
            cur.execute('INSERT INTO events (time, port, name, event_type, message) VALUES (?,?,?,?,?)',
                        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), port, name, event_type, message))
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"Log event failed: {e}")

    def get_config(self):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('SELECT key, value FROM config')
        rows = dict(c.fetchall())
        conn.close()
        return rows

    def set_config(self, key, value):
        try:
            conn = sqlite3.connect(self.db_file)
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
            conn = sqlite3.connect(self.db_file)
            cur = conn.cursor()
            cur.execute('SELECT date, total_traffic, total_in, total_out FROM daily ORDER BY date ASC LIMIT ?', (days,))
            rows = cur.fetchall()
            conn.close()
            return rows
        except Exception as e:
            log.error(f"Get daily failed: {e}")
            return []

    def save_daily(self, date, total, total_in, total_out, online, total_nodes):
        try:
            conn = sqlite3.connect(self.db_file)
            cur = conn.cursor()
            cur.execute('SELECT total_traffic, total_in, total_out FROM daily ORDER BY date DESC LIMIT 1')
            prev = cur.fetchone()
            if prev:
                if total >= prev[0] and total_in >= prev[1] and total_out >= prev[2]:
                    new_total, new_in, new_out = total, total_in, total_out
                else:
                    new_total = round(prev[0] + max(0, total), 1)
                    new_in = round(prev[1] + max(0, total_in), 1)
                    new_out = round(prev[2] + max(0, total_out), 1)
            else:
                new_total, new_in, new_out = total, total_in, total_out
            cur.execute('INSERT OR REPLACE INTO daily (date, total_traffic, total_in, total_out, online_count, total_nodes) VALUES (?,?,?,?,?,?)',
                        (date, new_total, new_in, new_out, online, total_nodes))
            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f"Save daily failed: {e}")
