import os
import tempfile
import unittest

from haproxy_ctl import HAProxyCtl
import reconcile


class _Config:
    haproxy_sock = "/tmp/tcp-panel-test.sock"
    haproxy_pid = "/tmp/tcp-panel-test.pid"

    def __init__(self, config_file):
        self.haproxy_cfg = config_file


class _Database:
    def __init__(self, rules):
        self._rules = rules

    def load(self):
        return self._rules


RULES = [{
    "name": "test",
    "local": "10000",
    "ip": "200.36.10.14",
    "port": "6011",
    "enable": 1,
    "quota": 0,
    "used": 0,
    "expire": "",
}]


class HAProxyConfigTests(unittest.TestCase):
    def setUp(self):
        fd, self.config_file = tempfile.mkstemp()
        os.close(fd)
        os.unlink(self.config_file)
        self.ctl = HAProxyCtl(_Config(self.config_file))

    def tearDown(self):
        if os.path.exists(self.config_file):
            os.unlink(self.config_file)

    def test_generate_config_contains_frontend_and_backend(self):
        text, ports, duplicates = self.ctl.generate_config(RULES)

        self.assertIn("frontend fe_10000", text)
        self.assertIn("backend be_10000", text)
        self.assertIn("server s10000 200.36.10.14:6011", text)
        self.assertEqual(ports, {"10000"})
        self.assertEqual(duplicates, {})

    def test_reconcile_requests_reload_when_config_is_missing(self):
        plan = reconcile.plan(_Database(RULES), self.ctl)

        self.assertEqual(plan["action"], "reload")
        self.assertEqual(plan["expected_ports"], 1)
        self.assertFalse(plan["config_identical"])


if __name__ == "__main__":
    unittest.main()
