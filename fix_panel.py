#!/usr/bin/env python3
"""Fix panel.py: add batch_add, fix port"""
with open('/root/tcp-panel-v2/panel.py') as f:
    src = f.read()

# 1. Add batch_add route after the add() function
old = '''    log.info(f"Added rule: {name} ({local} -> {ip}:{port})")
    return redirect(request.referrer or '/')

@app.route('/del/<int:idx>')'''

new = '''    log.info(f"Added rule: {name} ({local} -> {ip}:{port})")
    return redirect(request.referrer or '/')

@app.route('/batch_add', methods=['POST'])
@login_required
def batch_add():
    text = request.form.get('batch_data', '')
    rules = []
    for line in text.strip().split('\\n'):
        if not line or line.startswith('#'): continue
        parts = line.strip().split(':')
        if len(parts) < 4: continue
        name, local, ip, rport = parts[0], parts[1].strip(), parts[2], parts[3]
        ep = parts[4].strip() if len(parts) > 4 else ""
        qp = parts[5].strip() if len(parts) > 5 else ""
        if not name or not ip or not rport or not rport.isdigit(): continue
        if local and not local.isdigit(): continue
        if not local: local = haproxy.free_port()
        quota = 10.0 if qp in ('', '0') else float(qp) if qp else 10.0
        expire = haproxy.parse_expire(ep)
        rules.append({'name': name, 'local': local, 'ip': ip, 'port': rport,
                      'expire': expire, 'quota': quota, 'used': 0, 'enable': True})
    data = db.load()
    for r in rules:
        if any(i['local'] == r['local'] for i in data): continue
        data.append(r)
    db.save(data)
    haproxy.reload(data)
    log.info(f"Batch added {len(rules)} rules")
    return redirect(request.referrer or '/')

@app.route('/del/<int:idx>')'''

if old in src:
    src = src.replace(old, new)
    print("Added batch_add route")
else:
    print("SKIP batch_add - pattern not found")

# 2. Fix port to read from config
old_port = '''    # Force port 8081 for v14 test (separate from v13 on 8080)
    port = 8081'''
new_port = '''    port = int(db.get_config().get('panel_port', '8081'))'''
if old_port in src:
    src = src.replace(old_port, new_port)
    print("Port now reads from config")
else:
    print("SKIP port - pattern not found")

with open('/root/tcp-panel-v2/panel.py', 'w') as f:
    f.write(src)
print("Done")
