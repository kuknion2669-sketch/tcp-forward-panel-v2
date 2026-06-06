#!/usr/bin/env python3
"""Add batch import UI to index.html template"""
with open('/root/tcp-panel-v2/templates/index.html') as f:
    src = f.read()

old = '''        <a href="#" class="btn btn-gho" style="flex:1;text-align:center">取消</a>
      </div>
    </form>
  </div>
</div>'''

new = '''        <a href="#" class="btn btn-gho" style="flex:1;text-align:center">取消</a>
      </div>
    </form>
    <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--border)">
      <span onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display=='none'?'':'none'" class="btn btn-gho btn-sm" style="cursor:pointer;width:100%;text-align:center;display:block">批量导入</span>
      <div style="display:none;margin-top:8px">
        <form method="post" action="/batch_add">
        <div style="font-size:.7rem;color:var(--text2);margin-bottom:4px">每行一条，格式: 名称:本地端口:目标IP:目标端口</div>
        <textarea name="batch_data" rows="3" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border);background:rgba(0,0,0,0.4);color:var(--text);font-size:.75rem;font-family:monospace;outline:none;resize:vertical" placeholder="香港1:35957:1.2.3.4:443"></textarea>
        <button class="btn btn-pri btn-sm" type="submit" style="margin-top:6px">导入</button>
        </form>
      </div>
    </div>
  </div>
</div>'''

if old in src:
    src = src.replace(old, new)
    with open('/root/tcp-panel-v2/templates/index.html', 'w') as f:
        f.write(src)
    print("Added batch import UI")
else:
    print("Pattern not found")
