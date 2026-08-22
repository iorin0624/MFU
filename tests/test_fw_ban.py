import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import fw_ban
from utils.admin_logs_html import bind_runtime_csrf_token


def _load_remote_helper():
    path = ROOT / "deploy" / "mfu_fw_ban_ssh.py"
    spec = importlib.util.spec_from_file_location("mfu_fw_ban_ssh", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FwBanTests(unittest.TestCase):
    def test_normalize_ipv4_defaults_to_slash_24(self):
        self.assertEqual(
            fw_ban.normalize_ip_target(ip="203.0.113.45"),
            {"version": 4, "target": "203.0.113.0/24"},
        )

    @patch("utils.fw_ban.subprocess.run")
    def test_ssh_uses_mfu_identity_and_writable_home(self, run_mock):
        run_mock.return_value = subprocess.CompletedProcess([], 0, "ADDED", "")
        env = {
            "FW_BAN_IDENTITY_FILE": "/mnt/mfu/ssh/test-key",
            "FW_BAN_KNOWN_HOSTS": "/mnt/mfu/ssh/test-known-hosts",
            "FW_BAN_SSH_HOME": "/mnt/mfu/tmp",
        }
        with patch.dict(os.environ, env, clear=False):
            result = fw_ban.run_ssh_command("ban 4 203.0.113.0/24")

        self.assertTrue(result["ok"])
        command = run_mock.call_args.args[0]
        self.assertIn("/mnt/mfu/ssh/test-key", command)
        self.assertIn("UserKnownHostsFile=/mnt/mfu/ssh/test-known-hosts", command)
        self.assertEqual(run_mock.call_args.kwargs["env"]["HOME"], "/mnt/mfu/tmp")

    @patch("utils.fw_ban.run_ssh_command")
    def test_ban_uses_restricted_remote_command(self, run_mock):
        run_mock.return_value = {"ok": True, "status": "added"}
        fw_ban.ban_ip_cidr_via_ssh({"version": 6, "target": "2001:db8::1/128"})
        self.assertEqual(run_mock.call_args.args[0], "ban 6 2001:db8::1/128")

    def test_async_log_html_rebinds_csrf_token(self):
        source = '<meta name="csrf-token" content="job-token"><main>logs</main>'
        rebound = bind_runtime_csrf_token(source, 'browser-token')
        self.assertIn('content="browser-token"', rebound)
        self.assertNotIn('job-token', rebound)

    def test_remote_helper_rejects_other_commands(self):
        helper = _load_remote_helper()
        with self.assertRaises(ValueError):
            helper.parse_original_command("bash -c id")

    def test_remote_helper_accepts_canonical_ban(self):
        helper = _load_remote_helper()
        self.assertEqual(
            helper.parse_original_command("ban 4 203.0.113.44/24"),
            (4, "203.0.113.0/24", "badhosts4", "inet"),
        )


if __name__ == "__main__":
    unittest.main()
