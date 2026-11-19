"""Traffic statistics v2.

HAProxy cumulative byte counters are sampled and converted into deltas. The
deltas are accumulated in ``node_daily`` so daily/node totals are independent
of HAProxy reloads and never reconstructed by subtracting old total snapshots.
"""
import logging
import os
import time
from datetime import date as _date
from datetime import datetime, timedelta

log = logging.getLogger('stats')


def _display_mb(value):
    value = float(value or 0)
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.2f} TB"
    if value >= 1024:
        return f"{value / 1024:.2f} GB"
    return f"{value:.1f} MB"


class StatsCollector:
    def __init__(self, database, haproxy_ctl):
        self.db = database
        self.haproxy = haproxy_ctl
        self.last_update = 0.0
        self.last_result = {}
        try:
            self.interval = max(
                5, int(os.environ.get('TRAFFIC_SAMPLE_SECONDS', '15'))
            )
        except ValueError:
            self.interval = 15

    def load_last_state(self):
        """Compatibility hook; v2 keeps counters in SQLite, not JSON config."""
        return None

    def save_last_state(self):
        """Compatibility hook; v2 keeps counters in SQLite, not JSON config."""
        return None

    def clear_last(self, port):
        return self.db.clear_traffic_cursor(port)

    def update(self, force=False):
        now = time.time()
        if not force and now - self.last_update < self.interval:
            return self.last_result
        samples = self.haproxy.get_backend_stats()
        if not samples:
            return self.last_result
        observed_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        with self.db.locked():
            result = self.db.apply_traffic_snapshot(
                samples, observed_at=observed_at
            )
        if result is not None:
            self.last_result = result
            self.last_update = now
            if result.get('resets'):
                log.warning(
                    "HAProxy traffic counter reset detected on %s port(s)",
                    result['resets'],
                )
        return self.last_result

    def record(self):
        """Kept for worker compatibility; update already persists atomically."""
        return self.last_result

    def reset_epoch(self):
        samples = self.haproxy.get_backend_stats()
        if not samples:
            return None
        observed_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        with self.db.locked():
            epoch = self.db.reset_traffic_stats(
                samples, observed_at=observed_at
            )
        if epoch:
            self.last_update = time.time()
            self.last_result = {}
        return epoch

    def get_history(self, days=30):
        rows = self.db.get_traffic_daily_totals(days)
        by_date = {
            row[0]: {'in_mb': float(row[1] or 0), 'out_mb': float(row[2] or 0)}
            for row in rows
        }
        end = _date.today()
        start = end - timedelta(days=max(0, days - 1))
        history = []
        cursor = start
        while cursor <= end:
            day = cursor.isoformat()
            values = by_date.get(day, {'in_mb': 0.0, 'out_mb': 0.0})
            total = values['in_mb'] + values['out_mb']
            history.append({
                'date': day,
                'short_date': day[5:],
                'total': round(total, 3),
                'total_in': round(values['in_mb'], 3),
                'total_out': round(values['out_mb'], 3),
                'total_display': _display_mb(total),
                'total_in_display': _display_mb(values['in_mb']),
                'total_out_display': _display_mb(values['out_mb']),
            })
            cursor += timedelta(days=1)
        return history

    def get_hourly_history(self, selected_date, port=None):
        rows = self.db.get_traffic_hourly_totals(selected_date, port=port)
        by_hour = {
            int(row[0]): {
                'in_mb': float(row[1] or 0),
                'out_mb': float(row[2] or 0),
            }
            for row in rows
        }
        hourly = []
        for hour in range(24):
            values = by_hour.get(hour, {'in_mb': 0.0, 'out_mb': 0.0})
            total = values['in_mb'] + values['out_mb']
            hourly.append({
                'hour': hour,
                'label': f'{hour:02d}:00',
                'in_mb': round(values['in_mb'], 3),
                'out_mb': round(values['out_mb'], 3),
                'total_mb': round(total, 3),
                'in_display': _display_mb(values['in_mb']),
                'out_display': _display_mb(values['out_mb']),
                'total_display': _display_mb(total),
            })
        peak_rows = sorted(
            (row for row in hourly if row['total_mb'] > 0),
            key=lambda row: row['total_mb'],
            reverse=True,
        )[:3]
        peaks = [
            {
                'label': f"{row['label']}–{(row['hour'] + 1) % 24:02d}:00",
                'total_display': row['total_display'],
            }
            for row in peak_rows
        ]
        return hourly, peaks

    def get_dashboard(self, selected_date=None, days=30, query='', node=''):
        try:
            selected = datetime.strptime(selected_date, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            selected = _date.today()
        today = _date.today()
        if selected > today:
            selected = today

        selected_s = selected.isoformat()
        yesterday_s = (selected - timedelta(days=1)).isoformat()
        start7 = (selected - timedelta(days=6)).isoformat()
        start30 = (selected - timedelta(days=29)).isoformat()

        day_rows = {
            str(row['port']): row
            for row in self.db.get_traffic_nodes_for_date(selected_s)
        }
        yesterday_rows = {
            str(row['port']): row
            for row in self.db.get_traffic_nodes_for_date(yesterday_s)
        }
        period7 = {
            str(row[0]): row
            for row in self.db.get_traffic_totals_by_node(start7, selected_s)
        }
        period30 = {
            str(row[0]): row
            for row in self.db.get_traffic_totals_by_node(start30, selected_s)
        }

        rules = self.db.load()
        node_options = [
            {'port': str(rule.get('local', '')),
             'name': rule.get('name', '') or str(rule.get('local', ''))}
            for rule in rules if rule.get('local')
        ]
        rows = []
        current_ports = set()
        for rule in rules:
            port = str(rule.get('local', ''))
            if not port:
                continue
            current_ports.add(port)
            day = day_rows.get(port, {})
            yesterday = yesterday_rows.get(port, {})
            week = period7.get(port, (port, rule.get('name', ''), 0, 0))
            month = period30.get(port, (port, rule.get('name', ''), 0, 0))
            rows.append({
                'port': port,
                'name': rule.get('name', '') or day.get('name', ''),
                'ip': rule.get('ip', ''),
                'upstream_port': rule.get('port', ''),
                'group': rule.get('group_name', ''),
                'day_in_mb': round(float(day.get('in_mb', 0) or 0), 3),
                'day_out_mb': round(float(day.get('out_mb', 0) or 0), 3),
                'day_total_mb': round(
                    float(day.get('in_mb', 0) or 0)
                    + float(day.get('out_mb', 0) or 0), 3
                ),
                'yesterday_total_mb': round(
                    float(yesterday.get('in_mb', 0) or 0)
                    + float(yesterday.get('out_mb', 0) or 0), 3
                ),
                'week_total_mb': round(float(week[2] or 0) + float(week[3] or 0), 3),
                'month_total_mb': round(float(month[2] or 0) + float(month[3] or 0), 3),
                'last_seen': day.get('last_seen', ''),
            })

        for port, day in day_rows.items():
            if port in current_ports:
                continue
            rows.append({
                'port': port,
                'name': day.get('name', '') or '(已删除节点)',
                'ip': '',
                'upstream_port': '',
                'group': '',
                'day_in_mb': round(float(day.get('in_mb', 0) or 0), 3),
                'day_out_mb': round(float(day.get('out_mb', 0) or 0), 3),
                'day_total_mb': round(
                    float(day.get('in_mb', 0) or 0)
                    + float(day.get('out_mb', 0) or 0), 3
                ),
                'yesterday_total_mb': 0.0,
                'week_total_mb': 0.0,
                'month_total_mb': 0.0,
                'last_seen': day.get('last_seen', ''),
            })

        day_in = sum(float(row.get('in_mb', 0) or 0)
                     for row in day_rows.values())
        day_out = sum(float(row.get('out_mb', 0) or 0)
                      for row in day_rows.values())
        yesterday_total = sum(
            float(row.get('in_mb', 0) or 0)
            + float(row.get('out_mb', 0) or 0)
            for row in yesterday_rows.values()
        )
        week_total = sum(
            float(row[2] or 0) + float(row[3] or 0)
            for row in period7.values()
        )
        month_total = sum(
            float(row[2] or 0) + float(row[3] or 0)
            for row in period30.values()
        )

        needle = (query or '').strip().lower()
        if needle:
            rows = [
                row for row in rows
                if needle in row['name'].lower()
                or needle in row['port'].lower()
                or needle in row['ip'].lower()
            ]
        rows.sort(key=lambda row: (row['day_total_mb'], row['month_total_mb']),
                  reverse=True)
        for row in rows:
            row.update({
                'day_in_display': _display_mb(row['day_in_mb']),
                'day_out_display': _display_mb(row['day_out_mb']),
                'day_total_display': _display_mb(row['day_total_mb']),
                'yesterday_total_display': _display_mb(
                    row['yesterday_total_mb']
                ),
                'week_total_display': _display_mb(row['week_total_mb']),
                'month_total_display': _display_mb(row['month_total_mb']),
            })

        node_filter = str(node or '').strip()
        node_filter_name = ''
        if node_filter:
            for option in node_options:
                if option['port'] == node_filter:
                    node_filter_name = option['name']
                    break
            if not node_filter_name:
                for row in rows:
                    if row['port'] == node_filter:
                        node_filter_name = row['name']
                        break
        hourly, peak_hours = self.get_hourly_history(
            selected_s, port=node_filter or None
        )

        config = self.db.get_config()
        return {
            'rows': rows,
            'node_options': sorted(
                node_options, key=lambda item: item['name'].lower()
            ),
            'node_filter': node_filter,
            'node_filter_name': node_filter_name,
            'hourly': hourly,
            'peak_hours': peak_hours,
            'selected_date': selected_s,
            'yesterday_date': yesterday_s,
            'today': today.isoformat(),
            'epoch': config.get('traffic_stats_epoch', ''),
            'day_in': _display_mb(day_in),
            'day_out': _display_mb(day_out),
            'day_total': _display_mb(day_in + day_out),
            'yesterday_total': _display_mb(yesterday_total),
            'week_total': _display_mb(week_total),
            'month_total': _display_mb(month_total),
            'history': self.get_history(days=max(7, min(90, int(days)))),
        }
