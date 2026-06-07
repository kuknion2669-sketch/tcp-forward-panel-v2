# TCP Forward Panel v14

TCP 端口转发管理面板 — 基于 HAProxy 的一站式转发节点管理系统。

![Python](https://img.shields.io/badge/Python-3.9+-blue) ![Flask](https://img.shields.io/badge/Flask-3.x-lightgrey) ![HAProxy](https://img.shields.io/badge/HAProxy-2.2.9+-orange) ![License](https://img.shields.io/badge/License-MIT-green)

## 概述

通过 Web 界面管理 HAProxy TCP 转发规则。支持多节点、分组管理、流量统计、配额控制、一键检测等功能，适合自建代理中转服务。

## 架构

```
客户端 → 服务器:端口 → HAProxy → 落地机IP:端口
                ↑
          管理面板 (Flask + SQLite)
```

| 组件 | 技术栈 |
|------|--------|
| Web 框架 | Flask 3.x (Python) |
| 数据库 | SQLite (traffic.db) |
| 转发引擎 | HAProxy 2.2.9+ (TCP mode) |
| 前端 | 纯 HTML/CSS/JS，零 CDN 依赖 |
| 图表 | ECharts 本地加载 |
| 特效 | Three.js + Vanta 本地加载 |

## 功能特性

- **规则管理**：增/删/改/批量导入转发规则
- **分组视图**：按区域/用途自动分组，分组进度条
- **流量统计**：HAProxy stats 实时轮询，每日/30天趋势图
- **配额控制**：每节点配额设置，超额自动断连
- **到期管理**：节点到期自动停用
- **一键检测**：并发 30 线程检测所有节点连通性
- **实时开关**：滑动开关即时启用/停用节点，不触发全量 reload
- **连接状态**：查看 HAProxy 每端口当前连接数
- **安全认证**：Session 登录 + 密码 SHA256 加密
- **独立配置**：管理端口、账号密码在线修改
- **操作日志**：所有操作记录可追溯
- **数据导出**：一键导出所有规则备份

## 一键安装

```bash
curl -sL https://raw.githubusercontent.com/kuknion2669-sketch/tcp-forward-panel-v2/main/install.sh | bash
```

自定义参数：

```bash
# 改管理端口
curl -sL https://raw.githubusercontent.com/... | PANEL_PORT=9090 bash

# 改账号密码
curl -sL https://raw.githubusercontent.com/... | ADMIN_USER=myadmin ADMIN_PASS=MyPass123 bash
```

## 手动部署

**环境要求：** Debian 11/12, Python 3.9+, HAProxy 2.2+

```bash
# 1. 安装依赖
apt update && apt install -y python3 python3-pip haproxy socat git
pip3 install flask

# 2. 克隆代码
git clone https://github.com/kuknion2669-sketch/tcp-forward-panel-v2.git /root/tcp-panel-v2
cd /root/tcp-panel-v2

# 3. 初始化数据库（自动创建）
python3 -c "from database import Database; Database('/root/traffic.db')"

# 4. 启动面板
nohup python3 panel.py > /root/panel-v2.log 2>&1 &
```

## 面板界面

```
默认地址：http://服务器IP:8080
默认账号：admin
默认密码：admin123
```

## 配置 HAProxy 服务

```ini
[Unit]
Description=HAProxy Load Balancer
After=network-online.target

[Service]
Type=forking
ExecStart=/usr/sbin/haproxy -f /etc/haproxy/haproxy.cfg -p /run/haproxy.pid
ExecReload=/usr/sbin/haproxy -c -f /etc/haproxy/haproxy.cfg -q
ExecReload=/usr/sbin/haproxy -f /etc/haproxy/haproxy.cfg -p /run/haproxy.pid -sf $(cat /run/haproxy.pid 2>/dev/null)
Restart=always
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

## 项目结构

```
├── panel.py           主路由 + 登录认证
├── config.py          配置常量
├── database.py        SQLite 数据层
├── haproxy_ctl.py     HAProxy 管理（socket、配置生成、热加载）
├── stats_collector.py 流量统计（delta 计算、每日汇总、持久化）
├── check_mgr.py       节点连通性检测（ThreadPoolExecutor 并发）
├── templates/
│   ├── index.html     主面板（分组/全部视图）
│   ├── login.html     登录页
│   ├── settings.html  设置页
│   ├── edit.html      编辑页
│   ├── events.html    操作日志
│   └── haproxy.html   连接状态
└── static/
    ├── echarts.min.js     图表库
    ├── three.min.js       3D 引擎
    └── vanta.waves.min.js 波浪特效
```

## 常见问题

**Q: HAProxy reload 失败？**
原因：HAProxy 2.2.9 的 `-sf` 参数解析 PID 文件末尾换行符有 bug。面板使用 `systemctl restart haproxy` 作为 fallback。

**Q: 流量计数重启后丢失？**
修复：面板使用 `stats_last_traffic` 持久化到 SQLite config 表，重启自动恢复基线。

**Q: 端口冲突？**
面板自动检测端口占用并分配可用端口，也支持手动指定。

## License

MIT
