import os
import tempfile
import unittest

from database import Database


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
}


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)
        self.db = Database(self.path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm", ".lock"):
            try:
                os.unlink(self.path + suffix)
            except FileNotFoundError:
                pass

    def test_save_usage_does_not_rewrite_rule_fields(self):
        self.assertTrue(self.db.save([dict(RULE)]))
        data = self.db.load()
        data[0]["name"] = "edited"
        data[0]["used"] = 128
        data[0]["used_in"] = 64
        data[0]["used_out"] = 64

        self.assertTrue(self.db.save_usage(data))
        loaded = self.db.load()[0]

        self.assertEqual(loaded["name"], "test")
        self.assertEqual(loaded["used"], 128)

    def test_duplicate_local_port_is_rejected(self):
        self.assertTrue(self.db.save([dict(RULE)]))

        self.assertFalse(self.db.save([dict(RULE), dict(RULE, name="duplicate")]))

    def test_traffic_deltas_are_accumulated_per_node_and_day(self):
        self.assertTrue(self.db.save([dict(RULE)]))

        first = self.db.apply_traffic_snapshot(
            {"10000": {"bin": 10 * 1024 * 1024,
                       "bout": 20 * 1024 * 1024}},
            "2026-11-17 10:00:00",
        )
        self.assertEqual(first["ports"], 0)

        second = self.db.apply_traffic_snapshot(
            {"10000": {"bin": 11 * 1024 * 1024,
                       "bout": 22 * 1024 * 1024}},
            "2026-11-17 10:00:15",
        )
        self.assertEqual(second["in_mb"], 1.0)
        self.assertEqual(second["out_mb"], 2.0)
        loaded = self.db.load()[0]
        self.assertEqual(loaded["used_in"], 1.0)
        self.assertEqual(loaded["used_out"], 2.0)
        self.assertEqual(loaded["used"], 3.0)

        rows = self.db.get_traffic_nodes_for_date("2026-11-17")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["in_mb"], 1.0)
        self.assertEqual(rows[0]["out_mb"], 2.0)

    def test_counter_reset_counts_new_epoch_usage(self):
        self.assertTrue(self.db.save([dict(RULE)]))
        self.db.apply_traffic_snapshot(
            {"10000": {"bin": 100 * 1024 * 1024,
                       "bout": 200 * 1024 * 1024}},
            "2026-11-17 10:00:00",
        )

        result = self.db.apply_traffic_snapshot(
            {"10000": {"bin": 3 * 1024 * 1024,
                       "bout": 4 * 1024 * 1024}},
            "2026-11-17 10:00:15",
        )

        self.assertEqual(result["in_mb"], 3.0)
        self.assertEqual(result["out_mb"], 4.0)
        self.assertEqual(result["resets"], 2)

    def test_traffic_reset_clears_history_and_starts_new_baseline(self):
        self.assertTrue(self.db.save([dict(RULE)]))
        self.db.apply_traffic_snapshot(
            {"10000": {"bin": 1, "bout": 1}},
            "2026-11-17 10:00:00",
        )
        self.db.apply_traffic_snapshot(
            {"10000": {"bin": 2 * 1024 * 1024,
                       "bout": 2 * 1024 * 1024}},
            "2026-11-17 10:00:15",
        )

        epoch = self.db.reset_traffic_stats(
            {"10000": {"bin": 5 * 1024 * 1024,
                       "bout": 6 * 1024 * 1024}},
            "2026-11-17 10:00:30",
        )

        self.assertTrue(epoch)
        self.assertEqual(self.db.load()[0]["used"], 0)
        self.assertEqual(self.db.get_traffic_daily_totals(30), [])
        result = self.db.apply_traffic_snapshot(
            {"10000": {"bin": 6 * 1024 * 1024,
                       "bout": 8 * 1024 * 1024}},
            "2026-11-17 10:01:00",
        )
        self.assertEqual(result["in_mb"], 1.0)
        self.assertEqual(result["out_mb"], 2.0)

    def test_older_sample_cannot_overwrite_newer_cursor(self):
        self.assertTrue(self.db.save([dict(RULE)]))
        self.db.apply_traffic_snapshot(
            {"10000": {"bin": 1 * 1024 * 1024,
                       "bout": 1 * 1024 * 1024}},
            "2026-11-17 10:00:00.000000",
        )
        self.db.apply_traffic_snapshot(
            {"10000": {"bin": 3 * 1024 * 1024,
                       "bout": 3 * 1024 * 1024}},
            "2026-11-17 10:00:20.000000",
        )
        stale = self.db.apply_traffic_snapshot(
            {"10000": {"bin": 2 * 1024 * 1024,
                       "bout": 2 * 1024 * 1024}},
            "2026-11-17 10:00:10.000000",
        )

        self.assertEqual(stale["ports"], 0)
        self.assertEqual(self.db.load()[0]["used"], 4.0)


if __name__ == "__main__":
    unittest.main()
