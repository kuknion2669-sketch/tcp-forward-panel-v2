"""Traffic statistics - HAProxy stats polling, daily aggregation"""
import time
import logging
import json
from datetime import datetime

log = logging.getLogger('stats')

class StatsCollector:
    def __init__(self, database, haproxy_ctl):
        self.db = database
        self.haproxy = haproxy_ctl
        self.last_update = [0.0]
        self.last_traffic = {}

    def load_last_state(self):
        try:
            cfg = self.db.get_config()
            if 'stats_last_traffic' in cfg:
                loaded = json.loads(cfg['stats_last_traffic'])
                self.last_traffic.update(loaded)
                log.info(f"Loaded last_traffic with {len(loaded)} entries from DB")
        except Exception as e:
            log.warning(f"load_last_state: {e}")

    def save_last_state(self):
        try:
            encoded = json.dumps(self.last_traffic)
            self.db.set_config('stats_last_traffic', encoded)
        except Exception as e:
            log.warning(f"save_last_state: {e}")

    def clear_last(self, port):
        """Clear tracking for a single port (used after quota reset)"""
        if port in self.last_traffic:
            del self.last_traffic[port]

    def update(self):
        """Poll HAProxy stats, compute traffic deltas, save to DB"""
        now = time.time()
        if now - self.last_update[0] < 60:
            return
        self.last_update[0] = now

        cur = self.haproxy.get_backend_stats()
        if not cur:
            return

        data = self.db.load()
        changed = False

        for item in data:
            p = item.get('local')
            if not p or p not in cur:
                continue
            prev = self.last_traffic.get(p)
            c = cur[p]

            if not prev:
                item['used_in'] = round(c['bin'] / (1024*1024), 1)
                item['used_out'] = round(c['bout'] / (1024*1024), 1)
                item['used'] = round(item['used_in'] + item['used_out'], 1)
                changed = True
            else:
                d_in = c['bin'] - prev['bin']
                d_out = c['bout'] - prev['bout']
                if d_in >= 0 and d_out >= 0 and d_in < 10*1024*1024*1024 and d_out < 10*1024*1024*1024:
                    item['used_in'] = round(item.get('used_in', 0) + d_in / (1024*1024), 1)
                    item['used_out'] = round(item.get('used_out', 0) + d_out / (1024*1024), 1)
                    item['used'] = round(item.get('used_in', 0) + item.get('used_out', 0), 1)
                    changed = True
            self.last_traffic[p] = c

        if changed:
            self.db.save(data)

        self.save_last_state()
        self.record()

        # Auto-disable quota-exhausted nodes
        try:
            import socket as _sq
            _sq_s = _sq.socket(_sq.AF_UNIX, _sq.SOCK_STREAM)
            _sq_s.settimeout(3)
            _sq_s.connect(self.haproxy.sock)
            for it in data:
                q = it.get('quota', 0)
                if q > 0 and it.get('used', 0) >= q * 1024 and it.get('enable', True):
                    lo = it.get('local', '')
                    if lo:
                        _sq_s.sendall(f"disable server be_{lo}/s{lo}\n".encode())
                        _sq_s.sendall(f"shutdown sessions server be_{lo}/s{lo}\n".encode())
            _sq_s.close()
        except Exception as _e:
            log.warning(f"Auto-disable failed: {_e}")

    def record(self):
        """Save current traffic snapshot to daily table"""
        today = datetime.now().strftime('%Y-%m-%d')
        data = self.db.load()
        total = total_in = total_out = online = 0

        for item in data:
            p = item.get('local', '')
            if not p:
                continue
            try:
                total += float(item.get('used', 0))
                total_in += float(item.get('used_in', 0))
                total_out += float(item.get('used_out', 0))
            except Exception:
                pass  # non-critical, skip
            if self.haproxy.is_listening(p):
                online += 1

        self.db.save_daily(today, round(total, 1), round(total_in, 1),
                          round(total_out, 1), online, len(data))

    def get_history(self, days=30):
        """Get daily traffic history for chart display"""
        rows = self.db.get_daily(days)
        if not rows:
            return []

        prev_total = None
        prev_in = None
        prev_out = None
        max_daily = 1
        daily_data = []

        for row in rows:
            d, total, total_in, total_out = row[0], row[1], row[2], row[3]
            if prev_total is not None:
                daily = max(0, total - prev_total)
                daily_in = max(0, total_in - prev_in) if prev_in is not None else total_in
                daily_out = max(0, total_out - prev_out) if prev_out is not None else total_out
            else:
                daily = total
                daily_in = total_in
                daily_out = total_out
            prev_total, prev_in, prev_out = total, total_in, total_out
            daily_data.append({'date': d, 'daily': daily, 'daily_in': daily_in, 'daily_out': daily_out})
            if daily > max_daily:
                max_daily = daily

        history = []
        for entry in daily_data:
            d = entry['date']
            daily = entry['daily']
            daily_in = entry['daily_in']
            daily_out = entry['daily_out']
            bar_pct = int(daily / max_daily * 100) if max_daily > 0 else 0
            td = f"{round(daily/1024,1)} GB" if daily >= 1024 else f"{round(daily,1)} MB"
            tid = f"{round(daily_in/1024,1)} GB" if daily_in >= 1024 else f"{round(daily_in,1)} MB"
            tod = f"{round(daily_out/1024,1)} GB" if daily_out >= 1024 else f"{round(daily_out,1)} MB"
            history.append({
                'date': d, 'short_date': d[5:],
                'total_display': td, 'bar_pct': bar_pct,
                'total_in': round(daily_in, 1), 'total_out': round(daily_out, 1),
                'total_in_display': tid, 'total_out_display': tod
            })
        return history
