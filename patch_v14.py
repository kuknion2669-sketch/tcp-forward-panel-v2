#!/usr/bin/env python3
import os, py_compile

base = '/root/tcp-panel-v2'

# Fix 1: haproxy_ctl.py - reload() use systemctl restart
path = os.path.join(base, 'haproxy_ctl.py')
with open(path) as f:
    src = f.read()

old_reload = '''        # Hot reload
        try:
            if os.path.exists(self.pid_file):
                pid = open(self.pid_file).read().strip()
                subprocess.run(f"haproxy -f {self.config_file} -p {self.pid_file} -sf {pid}",
                             shell=True, capture_output=True)
            else:
                subprocess.run(f"haproxy -f {self.config_file} -p {self.pid_file} -D",
                             shell=True, capture_output=True)
        except Exception as e:
            log.error(f"Reload failed: {e}")'''

new_reload = '''        # Hot reload via systemctl
        try:
            subprocess.run('systemctl restart haproxy', shell=True, capture_output=True, timeout=10)
        except Exception as e:
            log.error(f"Reload failed: {e}")'''

if old_reload in src:
    src = src.replace(old_reload, new_reload, 1)
    with open(path, 'w') as f:
        f.write(src)
    print('Fix 1: haproxy_ctl.py reload() -> systemctl restart')
else:
    print('Fix 1 FAIL: old reload block not found')
    idx = src.find('# Hot reload')
    if idx >= 0:
        print(repr(src[idx:idx+500]))

try:
    py_compile.compile(path, doraise=True)
    print('  -> haproxy_ctl.py syntax OK')
except py_compile.PyCompileError as e:
    print(f'  -> haproxy_ctl.py syntax ERROR: {e}')

# ─── Fix 2: stats_collector.py ───
path = os.path.join(base, 'stats_collector.py')
with open(path) as f:
    src = f.read()

src = src.replace('import logging', 'import logging\nimport json')

old_init = '''    def clear_last(self, port):'''

new_methods = '''    def load_last_state(self):
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

    def clear_last(self, port):'''

if old_init in src:
    src = src.replace(old_init, new_methods, 1)
    print('Fix 2: save/load_last_state methods added')
else:
    print('Fix 2 FAIL: clear_last not found')

old_update = '''        if changed:
            self.db.save(data)

        self.record()'''
new_update = '''        if changed:
            self.db.save(data)

        self.save_last_state()
        self.record()'''

if old_update in src:
    src = src.replace(old_update, new_update, 1)
    print('Fix 2b: save_last_state() called in update()')
else:
    print('Fix 2b FAIL: update block not found')

with open(path, 'w') as f:
    f.write(src)
try:
    py_compile.compile(path, doraise=True)
    print('  -> stats_collector.py syntax OK')
except py_compile.PyCompileError as e:
    print(f'  -> stats_collector.py syntax ERROR: {e}')

# ─── Fix 3: panel.py - call load_last_state on startup ───
path = os.path.join(base, 'panel.py')
with open(path) as f:
    src = f.read()

old_startup = '''stats = StatsCollector(db, haproxy)'''
new_startup = '''stats = StatsCollector(db, haproxy)
stats.load_last_state()'''

if old_startup in src:
    src = src.replace(old_startup, new_startup, 1)
    with open(path, 'w') as f:
        f.write(src)
    print('Fix 3: load_last_state() called at startup')
else:
    print('Fix 3 FAIL: stats = StatsCollector line not found')

try:
    py_compile.compile(path, doraise=True)
    print('  -> panel.py syntax OK')
except py_compile.PyCompileError as e:
    print(f'  -> panel.py syntax ERROR: {e}')

print('\nAll v14 fixes applied')
