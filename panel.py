#!/usr/bin/env python3
"""TCP Forward Panel V2 正式版 1.0 — modular architecture"""
# v2 design principles:
# 1. Zero CDN dependency (loads in China without issues)
# 2. Module split (not 1700 lines single file)
# 3. Templates as separate files (not Python strings)
# 4. Proper error logging (no bare except: pass)
# 5. REST API layer for future SPA
# 6. Same DB schema → coexists with v13, no node disruption

from flask import Flask, render_template, request, redirect, Response, session, jsonify
import os, json, time, logging, secrets, socket, threading, subprocess
from datetime import datetime, timedelta
from functools import wraps
from logging.handlers import RotatingFileHandler

from config import Config, RESERVED_PORTS
from database import Database
from haproxy_ctl import HAProxyCtl, validate_rule_target
from check_mgr import CheckManager
from stats_collector import StatsCollector

_rh = RotatingFileHandler('/root/panel-v2.log', maxBytes=20*1024*1024, backupCount=5, encoding='utf-8')
_rh.setFormatter(logging.Formatter('%(asctime)s [%(name)s] %(levelname)s: %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[_rh])
log = logging.getLogger('panel')

app = Flask(__name__, template_folder='templates', static_folder='static')
_sk_path = '/root/tcp-panel-v2/.session_secret'
try:
    with open(_sk_path) as _f:
        _sk = _f.read().strip()
except Exception:
    _sk = secrets.token_hex(16)
    try:
        with open(_sk_path, 'w') as _f:
            _f.write(_sk)
        os.chmod(_sk_path, 0o600)
    except Exception:
        pass
app.secret_key = _sk
app.permanent_session_lifetime = timedelta(hours=24)

cfg = Config()
db = Database(cfg.db_file)
haproxy = HAProxyCtl(cfg)
checker = CheckManager(db)
stats = StatsCollector(db, haproxy)
stats.load_last_state()



def apply_desired(d, why=""):
    """把"期望状态"落地：写完数据库之后调用（2026-10-16 重构 #3）。

    老做法是"你这次请求自己生成配置 + 校验 + 重载 HAProxy"，页面因此要等 2-3 秒。
    新做法默认**异步**：只戳一下后台 worker（tcp-panel-worker.service），由它带锁地
    做 统计 / 自动停用 / 对账，页面立刻返回。

    应急回退：设环境变量 `PANEL_SYNC_APPLY=1` 就退回老的同步 reload 行为。
    """
    if os.environ.get("PANEL_SYNC_APPLY") == "1":
        try:
            return haproxy.reload(d)
        except Exception as e:                     # noqa: BLE001
            log.warning(f"sync apply failed: {e}")
            return False
    try:
        r = subprocess.run(
            ["systemctl", "start", "--no-block", "tcp-panel-worker.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3, start_new_session=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"systemctl returned {r.returncode}")
        log.info("apply_desired: poked worker%s", f" ({why})" if why else "")
        return True
    except Exception as e:                         # noqa: BLE001
        log.warning(f"poke worker failed: {e} -> 退回同步重载")
        try:
            return haproxy.reload(d)
        except Exception as e2:                    # noqa: BLE001
            log.warning(f"sync fallback failed: {e2}")
            return False


# ── Real-time node health cache & background jobs ──
CHECK_CACHE = {}
CHECK_CACHE_LOCK = threading.Lock()

def _cache_status(local):
    with CHECK_CACHE_LOCK:
        e = CHECK_CACHE.get(local)
    if e and time.time() - e['ts'] < 90:
        return e['ok']
    return None

def _refresh_check_cache(data, results):
    new_cache = {}
    for k, res in results.items():
        try:
            idx = int(k)
            lo = data[idx].get('local')
            if lo:
                new_cache[lo] = {'ok': bool(res.get('ok')), 'ts': time.time()}
        except Exception:
            pass
    with CHECK_CACHE_LOCK:
        CHECK_CACHE.update(new_cache)

def _enforce_auto_disable(data):
    """Auto-disable expired / quota-exhausted nodes in HAProxy AND in DB."""
    for item in data:
        if not item.get('enable', True):
            continue
        lo = item.get('local', '')
        if not lo:
            continue
        q = item.get('quota', 0)
        exhausted = q > 0 and item.get('used', 0) >= q * 1024
        expired = haproxy.is_expired(item.get('expire', ''))
        if not (exhausted or expired):
            continue
        reason = 'quota exhausted' if exhausted else 'expired'
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(3)
            s.connect(haproxy.sock)
            s.sendall(f"disable server be_{lo}/s{lo}\n".encode())
            s.sendall(f"shutdown sessions server be_{lo}/s{lo}\n".encode())
            s.close()
        except Exception as e:
            log.warning(f"Auto-disable socket {lo}: {e}")
        db.set_enable(lo, False)
        db.log_event(lo, item.get('name', ''), "auto_disable", reason)
        log.info(f"Auto-disabled {lo} ({item.get('name','')}): {reason}")

def _background_loop():
    while True:
        time.sleep(30)
        try:
            data = db.load()
            # Online/offline is now sourced from HAProxy's authoritative
            # `show stat` UP/DOWN (see enrich_item / server_status_map). The
            # separate bare-TCP probe was unreliable on flaky backends (it
            # timed out on nodes that were actively forwarding traffic), so we
            # no longer run it here. The manual "check" buttons still invoke
            # checker for on-demand diagnostics.
            _enforce_auto_disable(data)
            # Traffic stats must not depend on someone opening the dashboard.
            # update() is throttled to 60s internally; record() is cheap and
            # keeps the per-day delta (re)written so the rollover at midnight
            # is handled even if update() is within its throttle window.
            stats.update()
            stats.record()
        except Exception as e:
            log.warning(f"background loop error: {e}")

if os.environ.get('PANEL_BG_DISABLED') != '1':
    threading.Thread(target=_background_loop, daemon=True, name='panel-bg').start()

GROUP_LABELS = {
    "美区": "🇺🇸 美区", "香港": "🇭🇰 香港", "泰国": "🇹🇭 泰国",
    "马来": "🇲🇾 马来西亚", "日区": "🇯🇵 日区", "土耳": "🇹🇷 土耳其",
    "MY": "🇲🇾 马来西亚",
}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect("/login?next=" + request.path)
        return f(*args, **kwargs)
    return decorated

def detect_group(item):
    gn = item.get('group_name', '').strip()
    if gn:
        return gn
    name = item.get('name', '')
    for k in GROUP_LABELS:
        if name.startswith(k):
            return GROUP_LABELS[k]
    import re
    m = re.match(r'^([一-鿿]{2})', name)
    if m: return m.group(1)
    m = re.match(r'^([A-Za-z]+)', name)
    if m: return GROUP_LABELS.get(m.group(1).upper(), m.group(1).upper())
    return "📦 其他"

SENSITIVE_PORT_MIN = 1024

def is_sensitive_port(port_str):
    try:
        p = int(port_str)
        return p < SENSITIVE_PORT_MIN or p in RESERVED_PORTS
    except (ValueError, TypeError):
        return False

def valid_rule_target(local, ip, port):
    try:
        validate_rule_target(local, ip, port)
        return True
    except (TypeError, ValueError):
        return False

def enrich_item(item, idx, status_map):
    i = dict(item)
    i['_idx'] = idx
    # Use HAProxy's authoritative per-server status as the online source so
    # the badge matches what is actually being forwarded. Fall back to the
    # local-listener check only when HAProxy has no row for this local port
    # (e.g. a disabled rule that was left out of the config).
    # 2026-10-16: 老写法 `status_map.get(local, haproxy.is_listening(local))` 里 Python 的
    # 默认值是**无条件求值**的 —— 每一行规则都白跑一次 is_listening（老实现是 ~105ms 的
    # netstat 子进程）：198 行 ≈ 20 秒、分组视图跑两遍 ≈ 42 秒。改成显式判断，语义不变。
    i['online'] = (status_map[i['local']] if i['local'] in status_map
                   else haproxy.is_listening(i['local']))
    i['expired'] = haproxy.is_expired(i.get('expire', ''))
    i['expire_time_display'] = ''
    if i.get('expire'):
        try:
            from datetime import datetime
            i['expire_time_display'] = datetime.fromtimestamp(float(i['expire'])).strftime("%Y-%m-%d %H:%M:%S")
        except:
            pass
    ui = i.get('used_in', 0)
    uo = i.get('used_out', 0)
    i['used_in_display'] = f"{round(ui/1024,1)}GB" if ui >= 1024 else f"{round(ui,1)}MB"
    i['used_out_display'] = f"{round(uo/1024,1)}GB" if uo >= 1024 else f"{round(uo,1)}MB"
    i['group'] = detect_group(i)
    return i

# ─── Routes ───

@app.route('/')
@login_required
def index():
    q = request.args.get('q', '')
    sf = request.args.get('status', 'all')
    group_filter = request.args.get('group', '')
    view = request.args.get('view', 'groups') if not group_filter else 'detail'
    
    data = db.load()
    status_map = haproxy.server_status_map()
    enriched = [enrich_item(it, i, status_map) for i, it in enumerate(data)]
    
    total_q = sum(i.get('quota', 0) for i in enriched)
    total_u = sum(i.get('used', 0) for i in enriched)
    
    # Build groups
    groups = {}
    for item in enriched:
        g = item['group']
        if g not in groups:
            groups[g] = {'count': 0, 'used': 0, 'used_in': 0, 'used_out': 0,
                         'online': 0, 'offline': 0, 'expired': 0, 'quotaExhausted': 0, 'items': []}
        groups[g]['count'] += 1
        groups[g]['used'] += item.get('used', 0)
        groups[g]['used_in'] += item.get('used_in', 0)
        groups[g]['used_out'] += item.get('used_out', 0)
        if item.get('expired'):
            groups[g]['expired'] += 1
        elif item.get('quota', 0) > 0 and item.get('used', 0) >= item.get('quota', 0) * 1024:
            groups[g]['quotaExhausted'] += 1
        elif item.get('online'):
            groups[g]['online'] += 1
        else:
            groups[g]['offline'] += 1
        groups[g]['items'].append(item)
    
    sorted_groups = []
    for g_name in sorted(groups.keys()):
        nfo = groups[g_name]
        total_q_mb = sum(it.get('quota', 0) * 1024 for it in nfo['items'])
        bar = int(nfo['used'] / total_q_mb * 100) if total_q_mb > 0 else 0
        used_str = f"{round(nfo['used']/1024,1)} GB" if nfo['used'] >= 1024 else f"{round(nfo['used'],1)} MB"
        in_str = f"{round(nfo['used_in']/1024,1)} GB" if nfo['used_in'] >= 1024 else f"{round(nfo['used_in'],1)} MB"
        out_str = f"{round(nfo['used_out']/1024,1)} GB" if nfo['used_out'] >= 1024 else f"{round(nfo['used_out'],1)} MB"
        sorted_groups.append({
            'name': g_name, 'count': nfo['count'], 'bar_pct': bar,
            'used_str': used_str, 'in_str': in_str, 'out_str': out_str,
            'online': nfo['online'], 'offline': nfo['offline'],
            'expired': nfo['expired'], 'quotaExhausted': nfo['quotaExhausted']
        })
    
    # Filter
    filtered = []
    if view == 'all':
        for v in groups.values():
            filtered.extend(v['items'])
    elif group_filter and group_filter in groups:
        filtered = groups[group_filter]['items']
    if q:
        ql = q.lower()
        filtered = [i for i in filtered if ql in i['name'].lower() or ql in i['ip'].lower()]
    if sf != 'all':
        if sf == 'online':
            filtered = [i for i in filtered if i['online'] and not i['expired']]
        elif sf == 'offline':
            filtered = [i for i in filtered if not i['online'] and not i['expired']]
        elif sf == 'expired':
            filtered = [i for i in filtered if i['expired']]
        elif sf == 'quota':
            filtered = [i for i in filtered if i['quota'] > 0 and i['used'] >= i['quota'] * 1024]
    
    # History
    history = stats.get_history(days=30)
    
    total_used_str = f"{round(total_u/1024,1)} GB" if total_u >= 1024 else f"{round(total_u,1)} MB"
    
    return render_template('index.html',
        rule_count=len(enriched),
        total_quota=round(total_q, 1),
        total_used=total_used_str,
        sorted_groups=sorted_groups,
        filtered=filtered,
        all_nodes=enriched,
        view=view, group_filter=group_filter, q=q, sf=sf,
        history=history)

@app.route('/add', methods=['POST'])
@login_required
def add():
    name = request.form.get('name', '').strip()
    local = request.form.get('local', '').strip()
    ip = request.form.get('ip', '').strip()
    port = request.form.get('port', '').strip()
    if not name or not ip or not port:
        return redirect(request.referrer or '/')
    if not port.isdigit() or (local and not local.isdigit()):
        return redirect(request.referrer or '/')
    # `taken` = ports already owned by a rule in the DB. Auto-assignment must
    # respect it: a port that merely is not listening right now (HAProxy down /
    # reloading) is NOT free, it just looks free - that is how two rules ended
    # up on 10000 and HAProxy stopped starting (2026-10-15).
    with db.locked():
        data = db.load()
        taken = [d['local'] for d in data if d.get('local')]
        if local and is_sensitive_port(local):
            free = haproxy.free_port(taken)
            return redirect(
                (request.referrer or '/')
                + '?msg=sensitive_port&free=' + free
            )
        if not local:
            local = haproxy.free_port(taken)
            if not local:
                return redirect((request.referrer or '/') + '?msg=no_free_port')
        else:
            if local in taken:
                if is_sensitive_port(local):
                    local = haproxy.free_port(taken)
                    return redirect(
                        (request.referrer or '/') + "?msg=sensitive_port"
                    )
                local = haproxy.free_port(taken)
                return redirect(
                    (request.referrer or '/') + "?msg=port_taken&free=" + local
                )
        if not valid_rule_target(local, ip, port):
            return redirect((request.referrer or '/') + "?msg=invalid_target")
        expire = haproxy.parse_expire(request.form.get('expire', '').strip())
        quota_raw = request.form.get('quota', '').strip()
        try:
            quota = 0.0 if quota_raw in ('', '0') else float(quota_raw)
        except ValueError:
            return redirect((request.referrer or '/') + "?msg=invalid_quota")

        note = request.form.get('note', '').strip()
        group = request.form.get('group_name', '').strip()
        data.append({'name': name, 'local': local, 'ip': ip, 'port': port,
                     'expire': expire, 'quota': quota, 'used': 0, 'enable': True,
                     'note': note, 'group_name': group})
        if not db.save(data):
            return "保存规则失败", 500
    apply_desired(data)
    log.info(f"Added rule: {name} ({local} -> {ip}:{port})")
    db.log_event(local, name, "add", f"add: {local} -> {ip}:{port}")
    return redirect(request.referrer or '/')

@app.route('/batch_add', methods=['POST'])
@login_required
def batch_add():
    text = request.form.get('batch_data', '')
    with db.locked():
        rules = []
        data = db.load()
        taken = [d['local'] for d in data if d.get('local')]
        for line in text.strip().split('\n'):
            if not line or line.startswith('#'):
                continue
            parts = line.strip().split(':')
            if len(parts) < 4:
                continue
            name, local, ip, rport = (
                parts[0], parts[1].strip(), parts[2], parts[3].strip()
            )
            ep = parts[4].strip() if len(parts) > 4 else ""
            qp = parts[5].strip() if len(parts) > 5 else ""
            if not name or not ip or not rport:
                continue
            if local and (not local.isdigit() or is_sensitive_port(local)):
                continue
            if not local:
                local = haproxy.free_port(taken)
                if not local:
                    continue
                taken.append(local)
            elif local in taken:
                continue
            if not valid_rule_target(local, ip, rport):
                continue
            try:
                quota = 0.0 if qp in ('', '0') else float(qp)
            except ValueError:
                continue
            expire = haproxy.parse_expire(ep)
            rules.append({'name': name, 'local': local, 'ip': ip, 'port': rport,
                          'expire': expire, 'quota': quota, 'used': 0,
                          'enable': True, 'group_name': '', 'note': ''})
            taken.append(local)
        for r in rules:
            if any(i['local'] == r['local'] for i in data):
                continue
            data.append(r)
        if not db.save(data):
            return "批量保存规则失败", 500
    apply_desired(data)
    log.info(f"Batch added {len(rules)} rules")
    db.log_event("", "", "batch_add", f"batch add: {len(rules)} rules")
    return redirect(request.referrer or '/')

@app.route('/del/<local>')
@login_required
def delete(local):
    with db.locked():
        data = db.load()
        idx = next(
            (i for i, d in enumerate(data) if str(d.get('local')) == local),
            None,
        )
        ok = idx is not None
        name = ''
        if ok:
            name = data[idx].get('name', '')
            del data[idx]
            stats.clear_last(local)
            ok = db.save(data)
    if ok:
        apply_desired(data)
        log.info(f"Deleted rule #{idx}: {name}")
    db.log_event(local, name, "delete", f"delete: {local}")
    return jsonify({'ok': ok})

@app.route('/check/<local>')
@login_required
def check_node(local):
    data = db.load()
    idx = next(
        (i for i, d in enumerate(data) if str(d.get('local')) == local),
        None,
    )
    if idx is None:
        return jsonify({'ok': False, 'error': 'not found'})
    result = checker.check_one(idx, data)
    return jsonify(result)

@app.route('/check_all')
@login_required
def check_all():
    data = db.load()
    results = checker.check_all(data)
    _refresh_check_cache(data, results)
    return jsonify({'ok': True, 'results': results})

@app.route('/haproxy')
@login_required
def haproxy_page():
    nodes = db.load()
    node_map = {str(n['local']): n for n in nodes}
    stats_data = haproxy.get_stats()
    return render_template('haproxy.html', node_map=node_map, stats=stats_data)

@app.route('/api/connections')
@login_required
def api_connections():
    conns = haproxy.get_connections()
    return jsonify({'ok': True, 'ports': conns})

@app.route('/api/haproxy')
@login_required
def api_haproxy():
    result = haproxy.get_api_stats()
    return jsonify(result)

@app.route('/edit/<local>', methods=['GET', 'POST'])
@login_required
def edit(local):
    with db.locked():
        data = db.load()
        idx = next(
            (i for i, d in enumerate(data) if str(d.get('local')) == local),
            None,
        )
        if idx is None:
            return redirect('/')
        item = dict(data[idx])
        if request.method != 'POST':
            exp_dt = ''
            if item.get('expire'):
                try:
                    exp_dt = datetime.fromtimestamp(
                        float(item['expire'])
                    ).strftime('%Y-%m-%dT%H:%M')
                except (TypeError, ValueError, OSError):
                    pass
            return render_template('edit.html', item=item, expire=exp_dt)

        old = data[idx]
        auto_assigned = ''
        new_local = request.form.get('local', '').strip()
        new_name = request.form.get('name', '').strip()
        new_ip = request.form.get('ip', '').strip()
        new_port = request.form.get('port', '').strip()
        quota_raw = request.form.get('quota', '').strip()
        try:
            new_q = float(quota_raw) if quota_raw else old.get('quota', 0)
        except ValueError:
            return redirect(request.referrer or '/')
        if not new_name or not new_ip or not new_port:
            return redirect(request.referrer or '/')
        taken = [o.get('local') for oi, o in enumerate(data)
                 if oi != idx and o.get('local')]
        if not new_local.isdigit():
            return redirect(request.referrer or '/')
        if new_local != old.get("local", "") and is_sensitive_port(new_local):
            new_local = haproxy.free_port(taken)
            auto_assigned = 'sensitive'
        if new_local in taken:
            free = haproxy.free_port(taken)
            if not free or free in taken:
                return redirect("/?ecode=no_free_port")
            new_local = free
            auto_assigned = 'occupied'
        if not valid_rule_target(new_local, new_ip, new_port):
            return redirect(request.referrer or '/')
        ne = request.form.get('expire', '').strip()
        expire = haproxy.parse_expire(ne)
        new_note = request.form.get('note', '').strip()
        new_group = request.form.get('group_name', '').strip()
        old_local = old.get('local', '')
        if new_local != old_local:
            stats.clear_last(old_local)
            stats.clear_last(new_local)
        old.update({'name': new_name, 'local': new_local, 'ip': new_ip,
                    'port': new_port, 'expire': expire, 'quota': new_q,
                    'note': new_note, 'group_name': new_group})
        if not db.save(data):
            return "保存规则失败", 500
    apply_desired(data)
    log.info(f"Edited rule #{idx}: {new_name}")
    db.log_event(new_local, new_name, "edit", f"edit: {new_local}")
    if auto_assigned:
        return redirect("/?ecode=" + auto_assigned + "&free=" + new_local)
    return redirect(request.referrer or '/')

@app.route('/reset_quota/<local>')
@login_required
def reset_quota(local):
    with db.locked():
        data = db.load()
        idx = next(
            (i for i, d in enumerate(data) if str(d.get('local')) == local),
            None,
        )
        rname = ''
        if idx is not None:
            rname = data[idx].get('name', '')
            data[idx]['used'] = 0
            data[idx]['used_in'] = 0
            data[idx]['used_out'] = 0
            data[idx]['enable'] = True
            if not db.save(data):
                return "重置配额失败", 500
    if idx is not None:
        stats.clear_last(local)
        apply_desired(data)
        log.info(f"Reset quota for #{idx}: {rname}")
    db.log_event(local, rname, "reset_quota", f"reset_quota: {local}")
    return redirect(request.referrer or '/')

@app.route('/restart/<local>')
@login_required
def restart_node(local):
    data = db.load()
    for item in data:
        if item.get('local') == local:
            if haproxy.is_expired(item.get('expire', '')):
                return redirect(request.referrer or '/')
            if item.get('quota', 0) > 0 and item.get('used', 0) >= item.get('quota', 0) * 1024:
                return redirect(request.referrer or '/')
            apply_desired(data)
            break
    return redirect(request.referrer or '/')

@app.route('/api/toggle/<local>', methods=['POST'])
@login_required
def api_toggle(local):
    with db.locked():
        data = db.load()
        item = next(
            (d for d in data if str(d.get('local')) == local),
            None,
        )
        if item is None:
            return jsonify({'ok': False, 'error': 'not found'}), 404
        item['enable'] = not item.get('enable', True)
        enabled = item['enable']
        if not db.save(data):
            return jsonify({'ok': False, 'error': 'save failed'}), 500
    # Use HAProxy socket to enable/disable without a full config reload.
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(haproxy.sock)
        cmd = (f"enable server be_{local}/s{local}\n" if enabled
               else f"disable server be_{local}/s{local}\n")
        s.sendall(cmd.encode())
        s.close()
        log.info(f"Toggle {local}: {'enabled' if enabled else 'disabled'} via socket")
        db.log_event(
            local, item.get("name", ""), "toggle",
            f"toggle: {local} {'enabled' if enabled else 'disabled'}",
        )
    except Exception as e:
        log.warning(
            f"Socket toggle failed for {local}: {e}, falling back to reload"
        )
        apply_desired(data)
    return jsonify({'ok': True, 'enable': enabled})

@app.route('/backup')
@login_required
def backup():
    data = db.load()
    lines = [':'.join([d.get('name', ''), d.get('local', ''), d.get('ip', ''), d.get('port', '')]) for d in data]
    from datetime import datetime
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return Response('\n'.join(lines), mimetype='text/plain; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename=forward_backup_{ts}.txt'})

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        user = request.form.get('username', '')
        pwd = request.form.get('password', '')
        if db.check_auth(user, pwd):
            session.permanent = True
            session['user'] = user
            return redirect(request.args.get('next', '/'))
        error = '账号或密码错误'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect('/login')

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    cfg_data = db.get_config()
    msg = ''
    if request.method == 'POST':
        np = request.form.get('panel_port_v2', '').strip()
        nu = request.form.get('username', '').strip()
        pw = request.form.get('password', '')
        pw2 = request.form.get('password2', '')
        msg = '已保存'
        restart = False
        if np and np.isdigit():
            db.set_config('panel_port_v2', np)
            restart = True
        if nu:
            db.set_config('username', nu)
            session['user'] = nu
        if pw and pw == pw2:
            import hashlib
            db.set_config('password_hash', hashlib.sha256(pw.encode()).hexdigest())
        elif pw:
            msg = '两次密码不一致'
        cfg_data = db.get_config()
        if restart:
            import subprocess as _sp
            _sp.Popen("systemctl restart tcp-panel-v2", shell=True)
            return redirect(f'http://{request.host.rsplit(":", 1)[0]}:{np}/login')
    return render_template('settings.html', port=cfg_data.get('panel_port_v2', '8081'),
                          username=cfg_data.get('username', 'admin'), msg=msg)


@app.route('/events')
@login_required
def events_page():
    page = request.args.get('page', 1, type=int)
    limit = 50
    offset = (page - 1) * limit
    events_list = db.get_events(limit=limit, offset=offset)
    return render_template('events.html', events=events_list, page=page, limit=limit)


@app.before_request
def _v3_auth_guard():
    # Optional shared token for the internal v3 panel; no-op unless PANEL_V3_TOKEN set.
    if request.path.startswith('/api/v3/'):
        token = os.environ.get('PANEL_V3_TOKEN', '').strip()
        if token:
            hdr = request.headers.get('X-Panel-Token', '') or request.args.get('token', '')
            if hdr != token:
                return jsonify({'error': 'unauthorized'}), 403
    return None

@app.route('/audit')
@login_required
def audit():
    import subprocess
    hap_bytes = 0
    try:
        stats_raw = haproxy.get_api_stats()
        if stats_raw.get('ok'):
            for r in stats_raw.get('backends', []):
                try:
                    hap_bytes += int(r.get('bin', 0) or 0) + int(r.get('bout', 0) or 0)
                except Exception:
                    pass
    except Exception:
        pass
    haproxy_gb = round(hap_bytes / 1024 ** 3, 2)
    data = db.load()
    panel_gb = round(sum(float(d.get('used', 0) or 0) for d in data) / 1024.0, 2)
    iptables_gb = 0.0
    try:
        out = subprocess.getoutput('iptables -L -n -v -x 2>/dev/null')
        total = 0
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 7:
                try:
                    total += int(parts[1])
                except Exception:
                    pass
        iptables_gb = round(total / 1024 ** 3, 2)
    except Exception:
        pass
    eth0_rx = eth0_tx = 0
    try:
        with open('/proc/net/dev') as f:
            for line in f:
                if line.strip().startswith('eth0:'):
                    cols = line.split(':')[1].split()
                    eth0_rx = int(cols[0])
                    eth0_tx = int(cols[8])
                    break
    except Exception:
        pass
    eth0_total_gb = round((eth0_rx + eth0_tx) / 1024 ** 3, 2)
    eth0_rx_gb = round(eth0_rx / 1024 ** 3, 2)
    eth0_tx_gb = round(eth0_tx / 1024 ** 3, 2)
    return render_template('audit.html',
        haproxy_gb=haproxy_gb, iptables_gb=iptables_gb,
        panel_data=[{'gb': panel_gb}],
        eth0_total_gb=eth0_total_gb, eth0_rx_gb=eth0_rx_gb, eth0_tx_gb=eth0_tx_gb,
        daily_deltas=[], reset_time='')

@app.route('/api/reset_traffic', methods=['POST'])
@login_required
def reset_traffic():
    with db.locked():
        if not db.reset_usage():
            return jsonify({'ok': False, 'error': 'reset failed'}), 500
        data = db.load()
        stats.last_traffic.clear()
        stats.save_last_state()
    try:
        import subprocess as _sp
        _sp.run('iptables -Z', shell=True, timeout=10)
    except Exception:
        pass
    apply_desired(data)
    db.log_event('', '', 'reset_traffic', 'reset all traffic counters')
    return jsonify({'ok': True})

# === v3 API routes (no auth, for internal v3 panel on Reldens) ===
@app.route("/api/v3/rules", methods=["GET"])
def api_v3_rules():
    return jsonify(db.load())

@app.route("/api/v3/add", methods=["POST"])
def api_v3_add():
    body = request.get_json(force=True, silent=True)
    if not body: return jsonify({"error": "json body required"}), 400
    name = (body.get("name") or "").strip()
    ip = (body.get("ip") or "").strip()
    port = str(body.get("port") or "").strip()
    if not name or not ip or not port:
        return jsonify({"error": "name, ip, port required"}), 400
    local = str(body.get("local") or "").strip()
    with db.locked():
        data = db.load()
        taken = [d["local"] for d in data if d.get("local")]
        if not local or not local.isdigit():
            local = haproxy.free_port(taken)
            if not local:
                return jsonify({"error": "no free local port"}), 500
        elif is_sensitive_port(local):
            return jsonify({"error": "sensitive port"}), 400
        elif local in taken:
            local = haproxy.free_port(taken)
            if not local:
                return jsonify({"error": "no free local port"}), 500
        if not valid_rule_target(local, ip, port):
            return jsonify({"error": "invalid IP/port"}), 400
        expire = haproxy.parse_expire(body.get("expire", ""))
        quota_raw = body.get("quota", "")
        try:
            quota = 0.0 if quota_raw in ("", 0, "0") else float(quota_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid quota"}), 400
        note = body.get("note") or ""
        group = body.get("group_name") or ""
        data.append({
            "name": name, "local": local, "ip": ip, "port": port,
            "expire": expire, "quota": quota, "used": 0, "enable": True,
            "note": note, "group_name": group
        })
        if not db.save(data):
            return jsonify({"error": "save failed"}), 500
    apply_desired(data)
    db.log_event(local, name, "add", "v3 add: %s -> %s:%s" % (local, ip, port))
    return jsonify({"status": "ok", "local": local})

@app.route("/api/v3/del/<local>", methods=["POST"])
def api_v3_del(local):
    with db.locked():
        data = db.load()
        idx = next(
            (i for i, d in enumerate(data) if str(d.get("local")) == local),
            None,
        )
        if idx is None:
            return jsonify({"error": "not found"}), 404
        name = data[idx].get("name", "")
        data.pop(idx)
        stats.clear_last(local)
        if not db.save(data):
            return jsonify({"error": "save failed"}), 500
    apply_desired(data)
    db.log_event(local, name, "delete", "v3 delete: %s" % local)
    return jsonify({"status": "ok"})

@app.route("/api/v3/toggle/<local>", methods=["POST"])
def api_v3_toggle(local):
    with db.locked():
        data = db.load()
        item = next(
            (d for d in data if str(d.get("local")) == local),
            None,
        )
        if item is not None:
            item["enable"] = not item.get("enable", True)
            if not db.save(data):
                return jsonify({"error": "save failed"}), 500
    if item is not None:
        apply_desired(data)
        status = "enabled" if item["enable"] else "disabled"
        db.log_event(local, item.get("name", ""), "toggle", "v3 %s" % status)
        return jsonify({"status": status})
    return jsonify({"error": "not found"}), 404

@app.route("/api/v3/reload", methods=["POST"])
def api_v3_reload():
    data = db.load()
    apply_desired(data)
    return jsonify({"status": "ok"})


if __name__ == '__main__':
    port = int(db.get_config().get('panel_port_v2', '8081'))
    log.info(f"Starting panel v2 on port {port}")
    print(f"Panel v2 starting on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
