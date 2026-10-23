#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""对账程序：让 HAProxy 实际在跑的 = 数据库里的"期望状态"。

为什么要有它（2026-10-16 重构 #3）：
  老流程是"你在页面点保存 → 你这次请求自己生成配置、校验、重载 HAProxy"，
  页面因此要等 2-3 秒（改完还要再等校验）。
  新流程：页面只写数据库（毫秒级返回），由本程序把差异补到 HAProxy 上。
  额外好处：①HAProxy 被人工改乱、或它自己重启过，会被自动补回一致；
  ②每次动作都留下记录（state/reconcile_state.json + 日志），可追溯。

用法：
  python3 reconcile.py            # 对账并应用（**内容有差异才** reload）
  python3 reconcile.py --check    # 干跑：只报告差异，不动 HAProxy
  python3 reconcile.py --force    # 内容一致也强制 reload
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from config import Config            # noqa: E402
from database import Database        # noqa: E402
from haproxy_ctl import HAProxyCtl   # noqa: E402

STATE_FILE = os.path.join(HERE, "state", "reconcile_state.json")
log = logging.getLogger("reconcile")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def plan(db, haproxy, force=False):
    """算出该做什么。返回 dict（action = none / reload / error）。"""
    data = db.load()
    try:
        cfg_text, expected_ports, dups = haproxy.generate_config(data)
    except ValueError as e:
        return {"action": "error", "reason": str(e), "rules": len(data)}
    if dups:
        return {"action": "error",
                "reason": "同一本地端口被多条规则占用：%s" % dups,
                "rules": len(data)}
    try:
        with open(haproxy.config_file, encoding="utf-8") as fh:
            current = fh.read()
    except FileNotFoundError:
        current = None
    same = (
        current == cfg_text
        and haproxy.applied_config_matches(cfg_text)
    )
    return {
        "action": "none" if (same and not force) else "reload",
        "rules": len(data),
        "expected_ports": len(expected_ports),
        "config_identical": same,
        "data": data,
    }


def apply_plan(db, haproxy, p):
    """按 plan 执行；返回 (ok, detail)。"""
    if p["action"] == "none":
        return True, "配置与期望一致，无需动作"
    if p["action"] == "error":
        return False, p["reason"]
    ok = haproxy.reload(p["data"])
    return bool(ok), ("已重载 HAProxy（%d 条规则 / %d 个端口）"
                      % (p["rules"], p["expected_ports"]) if ok
                      else "重载失败（看日志 /var/log 与 journalctl -u haproxy）")


def write_state(rec):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
    except Exception as e:                    # noqa: BLE001
        log.warning("写状态文件失败: %s", e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="干跑，不动 HAProxy")
    ap.add_argument("--force", action="store_true", help="内容一致也强制 reload")
    ap.add_argument("--json", action="store_true", help="只打印结果 JSON")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    db = Database(cfg.db_file)
    haproxy = HAProxyCtl(cfg)

    t0 = time.time()
    p = plan(db, haproxy, force=args.force)
    rec = {"at": _now(), "action": p["action"], "rules": p.get("rules"),
           "expected_ports": p.get("expected_ports"),
           "config_identical": p.get("config_identical"),
           "dry_run": bool(args.check)}
    if args.check:
        rec["ok"] = (p["action"] != "error")
        rec["detail"] = "干跑：" + ("需要 reload" if p["action"] == "reload"
                                   else "无需动作")
    else:
        ok, detail = apply_plan(db, haproxy, p)
        rec["ok"] = ok
        rec["detail"] = detail
    rec["duration_s"] = round(time.time() - t0, 3)
    write_state(rec)
    if args.json:
        print(json.dumps(rec, ensure_ascii=False))
    else:
        print("[reconcile] %s | action=%s ok=%s rules=%s ports=%s %.3fs | %s"
              % (rec["at"], rec["action"], rec["ok"], rec["rules"],
                 rec["expected_ports"], rec["duration_s"], rec["detail"]))
    return 0 if rec.get("ok") else 3


if __name__ == "__main__":
    sys.exit(main())
