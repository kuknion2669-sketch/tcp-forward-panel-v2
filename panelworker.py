#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""面板"后台活"（2026-10-16 从 Web 进程里拆出来的那一份）。

为什么拆：以前 Web 进程和后台活（流量统计 / 自动停用）绑在一个进程里，
所以只能开 1 个 gunicorn worker —— 多开就会**各跑一份统计**，流量被重复计数、
可能误判"超配额"把节点关掉；而只有 1 个 worker 时，一个人点慢了别人全排队。

现在：本程序由 systemd timer 每 15 秒拉起一次（也会被页面写入"戳"一下立刻跑），
干三件事（顺序固定）：
  1. 流量统计（stats.update + stats.record，内部自带 60 秒节流）
  2. 自动停用（超配额 / 已过期 → 关掉；走 HAProxy 运行时命令，不需 reload）
  3. 对账（reconcile：把 HAProxy 拉回和数据库一致，有差异才 reload）

同一时刻只允许一份在跑（文件锁 /run/tcp-panel-worker.lock）—— 这正是
Web 侧可以安全开多个 worker 的前提。

用法：
  python3 panelworker.py             # 干全套（timer 与页面戳都走这个）
  python3 panelworker.py --stats-only
  python3 panelworker.py --dry-run   # 只报告不动作（不写库、不 reload）
"""
import argparse
import fcntl
import json
import logging
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("PANEL_BG_DISABLED", "1")   # 别在 worker 里再起一个 web 后台线程

LOCK_PATH = "/run/tcp-panel-worker.lock"
LOOP_MAX = 3          # 一轮里最多连续对账几次（写入比读取快时多跑一次就收敛）
STATE_FILE = os.path.join(HERE, "state", "worker_state.json")
log = logging.getLogger("panelworker")


def _lock():
    """拿独占锁；拿不到（已有一份在跑）返回 None。"""
    try:
        fh = open(LOCK_PATH, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats-only", action="store_true", help="只跑流量统计")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库不 reload")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    fh = _lock()
    if fh is None:
        log.info("已有一份 worker 在跑，本次跳过")
        return 0

    t0 = time.time()
    rec = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pid": os.getpid(),
           "dry_run": bool(args.dry_run)}
    try:
        import panel            # 复用面板自己的 db/haproxy/stats 与内部逻辑
        import reconcile

        # 1) 流量统计（dry-run 时只 load，不动库）
        if args.dry_run:
            rec["stats"] = "skipped(dry-run)"
        else:
            panel.stats.update()
            panel.stats.record()
            rec["stats"] = "ok"

        # 2) 自动停用（超配额 / 过期）
        if args.dry_run:
            rec["enforce"] = "skipped(dry-run)"
        else:
            panel._enforce_auto_disable(panel.db.load())
            rec["enforce"] = "ok"

        if args.stats_only:
            rec["reconcile"] = "skipped(--stats-only)"
        else:
            # 3) 对账：最多连做 LOOP_MAX 次直到"配置 = 期望"（页面刚写完的情况）
            acts = []
            for _ in range(LOOP_MAX):
                p = reconcile.plan(panel.db, panel.haproxy)
                if p["action"] == "none":
                    break
                if args.dry_run:
                    acts.append({k: p[k] for k in ("action", "rules",
                                                   "expected_ports",
                                                   "config_identical")})
                    break
                ok, detail = reconcile.apply_plan(panel.db, panel.haproxy, p)
                acts.append({"action": p["action"], "ok": ok, "detail": detail})
                reconcile.write_state({
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "action": p["action"], "rules": p["rules"],
                    "expected_ports": p["expected_ports"],
                    "config_identical": p["config_identical"],
                    "dry_run": False, "ok": ok, "detail": detail,
                    "by": "panelworker",
                })
                if not ok:
                    break
            rec["reconcile"] = acts or [{"action": "none"}]
    except Exception as e:                                    # noqa: BLE001
        log.exception("worker 出错")
        rec["error"] = str(e)[:300]
    finally:
        rec["duration_s"] = round(time.time() - t0, 3)
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False, indent=2)
        except Exception:                                     # noqa: BLE001
            pass
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        except Exception:                                     # noqa: BLE001
            pass
    log.info("worker 完成 %.3fs: %s", rec["duration_s"],
             json.dumps({k: v for k, v in rec.items()
                         if k in ("stats", "enforce", "reconcile", "error")},
                        ensure_ascii=False)[:300])
    return 0 if "error" not in rec else 3


if __name__ == "__main__":
    sys.exit(main())
