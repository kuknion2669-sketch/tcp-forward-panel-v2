import os
import tempfile
import unittest
from datetime import datetime, timedelta

from database import Database
from node_hygiene import cleanup_offline_nodes


RULE = {
    "name": "test",
    "local": "10000",
    "ip": "200.36.10.14",
    "port": "6011",
    "expire": "",
    "quota": 0,
    "used": 0,
    "used_in": 0,
    "used_out": 0,
    "enable": True,
    "note": "",
    "group_name": "",
    "offline_since": "",
}


class _Haproxy:
    def __init__(self, status):
        self.status = status

    def server_status_map(self, force=False):
        return dict(self.status)


class OfflineCleanupTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.db = Database(self.path)
        self.now = datetime(2026, 11, 19, 12, 0, 0)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm", ".lock"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_online_node_resets_offline_timestamp(self):
        old = (self.now - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(self.db.save([dict(RULE, offline_since=old)]))

        result = cleanup_offline_nodes(
            self.db, _Haproxy({"10000": True}), now=self.now
        )

        self.assertEqual(result["online_reset"], 1)
        self.assertEqual(self.db.load()[0]["offline_since"], "")

    def test_enabled_node_offline_over_60_days_is_deleted(self):
        old = (self.now - timedelta(days=61)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(self.db.save([dict(RULE, offline_since=old)]))

        result = cleanup_offline_nodes(
            self.db, _Haproxy({"10000": False}), now=self.now
        )

        self.assertEqual(len(result["deleted"]), 1)
        self.assertEqual(self.db.load(), [])
        events = self.db.get_events()
        self.assertEqual(events[0]["event_type"], "auto_delete_offline")

    def test_disabled_node_is_never_deleted(self):
        old = (self.now - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S")
        self.assertTrue(self.db.save([
            dict(RULE, enable=False, offline_since=old)
        ]))

        result = cleanup_offline_nodes(
            self.db, _Haproxy({"10000": False}), now=self.now
        )

        self.assertEqual(result["deleted"], [])
        self.assertEqual(len(self.db.load()), 1)
        self.assertEqual(self.db.load()[0]["offline_since"], "")

    def test_new_offline_starts_tracking_without_deleting(self):
        self.assertTrue(self.db.save([dict(RULE)]))

        result = cleanup_offline_nodes(
            self.db, _Haproxy({"10000": False}), now=self.now
        )

        self.assertEqual(result["newly_offline"], 1)
        self.assertEqual(result["deleted"], [])
        self.assertEqual(
            self.db.load()[0]["offline_since"],
            "2026-11-19 12:00:00",
        )

    def test_dry_run_does_not_change_database(self):
        self.assertTrue(self.db.save([dict(RULE)]))

        result = cleanup_offline_nodes(
            self.db,
            _Haproxy({"10000": False}),
            now=self.now,
            dry_run=True,
        )

        self.assertTrue(result["dry_run"])
        self.assertEqual(self.db.load()[0]["offline_since"], "")


if __name__ == "__main__":
    unittest.main()
