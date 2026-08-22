import importlib.util
import sys
import types
import unittest
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parents[1] / "phone_whitelist"
package = types.ModuleType("phone_whitelist")
package.__path__ = [str(PACKAGE_DIR)]
sys.modules.setdefault("phone_whitelist", package)

for module_name in ("service", "action_token"):
    path = PACKAGE_DIR / f"{module_name}.py"
    spec = importlib.util.spec_from_file_location(f"phone_whitelist.{module_name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

token_service = sys.modules["phone_whitelist.action_token"]


class PhoneBlacklistActionTokenTest(unittest.TestCase):
    SECRET = "11" * 32

    def test_issue_and_validate(self):
        token = token_service.issue_action_token("+81 80 9324 2655", self.SECRET, now=1000)
        claims = token_service.validate_action_token(token, self.SECRET, now=1200)
        self.assertEqual(claims.phone_number, "08093242655")
        self.assertEqual(claims.expires_at, 2800)
        self.assertEqual(len(token_service.token_fingerprint(token)), 64)

    def test_expired(self):
        token = token_service.issue_action_token("08093242655", self.SECRET, now=1000)
        with self.assertRaises(token_service.ActionTokenExpired):
            token_service.validate_action_token(token, self.SECRET, now=2801)

    def test_reject_tampering(self):
        token = token_service.issue_action_token("08093242655", self.SECRET, now=1000)
        body, signature = token.split(".", 1)
        with self.assertRaises(token_service.ActionTokenError):
            token_service.validate_action_token(f"{body}x.{signature}", self.SECRET, now=1200)

    def test_reject_wrong_secret(self):
        token = token_service.issue_action_token("08093242655", self.SECRET, now=1000)
        with self.assertRaises(token_service.ActionTokenError):
            token_service.validate_action_token(token, "22" * 32, now=1200)


if __name__ == "__main__":
    unittest.main()
