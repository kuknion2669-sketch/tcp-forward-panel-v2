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


if __name__ == "__main__":
    unittest.main()
