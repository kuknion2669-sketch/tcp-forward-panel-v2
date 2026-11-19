"""Automatic cleanup for enabled nodes that stay offline for too long."""
import logging
import os
from datetime import datetime, timedelta


log = logging.getLogger("node-hygiene")
DELETE_AFTER_DAYS = max(1, int(os.environ.get("AUTO_DELETE_OFFLINE_DAYS", "60")))


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def cleanup_offline_nodes(db, haproxy, now=None, dry_run=False):
    """Track continuous OFF status and delete enabled nodes after the threshold.

    Disabled nodes are intentionally excluded: a manual disable is not an
    automatic-offline signal and must never become a deletion candidate.
    """
    now = now or datetime.now()
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    cutoff = now - timedelta(days=DELETE_AFTER_DAYS)
    status_map = haproxy.server_status_map(force=True)
    result = {
        "days": DELETE_AFTER_DAYS,
        "checked": 0,
        "newly_offline": 0,
        "online_reset": 0,
        "candidates": [],
        "deleted": [],
    }

    with db.locked():
        data = db.load()
        changed = False
        for item in data:
            local = str(item.get("local", ""))
            if not local:
                continue
            if not item.get("enable", True):
                if item.get("offline_since"):
                    item["offline_since"] = ""
                    changed = True
                continue

            result["checked"] += 1
            online = bool(status_map.get(local, False))
            if online:
                if item.get("offline_since"):
                    item["offline_since"] = ""
                    result["online_reset"] += 1
                    changed = True
                continue

            offline_since = _parse_time(item.get("offline_since"))
            if not item.get("offline_since") or offline_since is None:
                item["offline_since"] = now_text
                result["newly_offline"] += 1
                changed = True
                continue
            if offline_since <= cutoff:
                result["candidates"].append({
                    "name": item.get("name", ""),
                    "local": local,
                    "offline_since": item.get("offline_since", ""),
                })

        if dry_run:
            result["dry_run"] = True
            return result

        if changed and not db.save(data):
            raise RuntimeError("failed to save offline tracking state")

        if result["candidates"]:
            ports = {item["local"] for item in result["candidates"]}
            remaining = [
                item for item in data
                if str(item.get("local", "")) not in ports
            ]
            for item in result["candidates"]:
                db.log_event(
                    item["local"],
                    item["name"],
                    "auto_delete_offline",
                    f"offline since {item['offline_since']} (> "
                    f"{DELETE_AFTER_DAYS} days)",
                )
            result["deleted"] = list(result["candidates"])
            if not db.save(remaining):
                raise RuntimeError("failed to delete offline nodes")
            log.warning(
                "Deleted %d node(s) offline for more than %d days",
                len(result["deleted"]),
                DELETE_AFTER_DAYS,
            )
    return result
