import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

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
        self.ctl.applied_hash_file = self.config_file + ".applied"

    def tearDown(self):
        if os.path.exists(self.config_file):
            os.unlink(self.config_file)
        if os.path.exists(self.ctl.applied_hash_file):
            os.unlink(self.ctl.applied_hash_file)

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

    def test_reconcile_is_clean_only_after_config_was_applied(self):
        text, _, _ = self.ctl.generate_config(RULES)
        self.ctl._write_config(text)

        self.assertEqual(
            reconcile.plan(_Database(RULES), self.ctl)["action"],
            "reload",
        )

        self.ctl._mark_config_applied(text)
        self.assertEqual(
            reconcile.plan(_Database(RULES), self.ctl)["action"],
            "none",
        )

    def test_generate_config_rejects_config_injection(self):
        bad = dict(RULES[0], ip="127.0.0.1\nserver injected 1.2.3.4:80")

        with self.assertRaises(ValueError):
            self.ctl.generate_config([bad])

    def test_generate_config_rejects_invalid_port(self):
        bad = dict(RULES[0], port="443\nserver injected 1.2.3.4:80")

        with self.assertRaises(ValueError):
            self.ctl.generate_config([bad])

    def test_reconcile_reports_invalid_rules(self):
        bad = dict(RULES[0], ip="")

        plan = reconcile.plan(_Database([bad]), self.ctl)

        self.assertEqual(plan["action"], "error")
        self.assertIn("upstream host", plan["reason"])


class ReloadRollbackTests(unittest.TestCase):
    def setUp(self):
        fd, self.config_file = tempfile.mkstemp(suffix=".cfg")
        os.close(fd)
        with open(self.config_file, "w", encoding="utf-8") as f:
            f.write("OLD\n")
        self.ctl = HAProxyCtl(_Config(self.config_file))
        self.ctl.applied_hash_file = self.config_file + ".applied"
        self.ctl.validate_config = lambda text: (True, "ok")
        self.ctl._reserve_frontend_ports = lambda ports: None
        self.ctl._haproxy_process_state = lambda: (100, {101})

    def tearDown(self):
        for path in (self.config_file, self.ctl.applied_hash_file):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def test_failed_reload_restores_previous_config(self):
        failed = SimpleNamespace(returncode=1, stderr=b"reload failed")
        with mock.patch("haproxy_ctl.subprocess.run", return_value=failed):
            ok = self.ctl.reload([dict(RULES[0])])

        self.assertFalse(ok)
        with open(self.config_file, encoding="utf-8") as f:
            self.assertEqual(f.read(), "OLD\n")

    def test_successful_runtime_check_marks_exact_config_applied(self):
        self.ctl._runtime_applied = lambda ports, state: True
        success = SimpleNamespace(returncode=0, stderr=b"")
        with mock.patch("haproxy_ctl.subprocess.run", return_value=success):
            ok = self.ctl.reload([dict(RULES[0])])

        self.assertTrue(ok)
        with open(self.config_file, encoding="utf-8") as f:
            applied_text = f.read()
        self.assertTrue(self.ctl.applied_config_matches(applied_text))


if __name__ == "__main__":
    unittest.main()
