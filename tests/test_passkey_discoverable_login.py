"""ID を入力しない WebAuthn ログインの回帰テスト（外部サービス不要）。"""

import ast
import base64
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "routes" / "webauthn_routes.py").read_text(encoding="utf-8")
FUNCTIONS = {
    "_b64url_encode", "_b64url_decode", "_store_challenge", "_load_challenge",
    "_clear_challenge", "auth_options", "auth_verify",
}


class Session(dict):
    permanent = False


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""
        self.params = ()

    def execute(self, query, params):
        self.query = query
        self.params = params

    def fetchall(self):
        if "FROM user_passkeys" in self.query:
            return [row for row in self.rows if row["credential_id"] == self.params[0]][:2]
        return []

    def fetchone(self):
        if "FROM user_passkeys" in self.query:
            return next(
                (row for row in self.rows if row["username"] == self.params[0]
                 and row["credential_id"] == self.params[1]),
                None,
            )
        return None


class FakeDB:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self, **_kwargs):
        return self.cur

    def commit(self):
        pass

    def close(self):
        pass


class DiscoverablePasskeyLoginTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE)
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
        # Flask の decorator を外し、ルート関数そのものをテストする。
        for node in selected:
            node.decorator_list = []
        module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
        self.session = Session()
        self.rows = [{
            "id": 1, "username": "alice", "credential_id": "credential-1",
            "public_key": base64.urlsafe_b64encode(b"key").decode(), "sign_count": 0,
        }]
        self.payload = {}
        self.verify_calls = []
        self.login_logs = []
        self.admin_sessions = []
        self.ns = {
            "base64": base64, "os": os, "time": time, "session": self.session,
            "request": SimpleNamespace(get_json=lambda **_kw: self.payload, remote_addr="127.0.0.1"),
            "jsonify": lambda **data: data,
            "current_app": SimpleNamespace(logger=SimpleNamespace(exception=lambda *_args: None)),
            "CHALLENGE_TTL_SECONDS": 120, "RP_ID": "mfu.iori0624.jp",
            "ORIGIN": "https://mfu.iori0624.jp", "ADMIN_USERNAME": "admin",
            "STEPUP_PURPOSE": "admin_action", "QR_APPROVAL_PURPOSE": "admin_qr_approval",
            "get_db": lambda: FakeDB(self.rows),
            "_fetch_user": lambda name: {"nickname": name} if name in {"alice", "admin"} else None,
            "_log_webauthn_request": lambda *_args, **_kw: None,
            "_log_credential_shape": lambda *_args, **_kw: None,
            "write_auth_audit_log": lambda *_args, **_kw: None,
            "write_login_log": lambda *args, **_kw: self.login_logs.append(args),
            "audit": lambda *_args, **_kw: None,
            "password_preauth_valid": lambda _name: False,
            "establish_admin_session": lambda **kwargs: self.admin_sessions.append(kwargs),
            "verify_authentication_response": self.fake_verify,
            "_user_verification": lambda _name: "preferred",
            "UserVerificationRequirement": SimpleNamespace(REQUIRED="required"),
        }
        exec(compile(module, str(ROOT / "routes" / "webauthn_routes.py"), "exec"), self.ns)

    def fake_verify(self, **kwargs):
        self.verify_calls.append(kwargs)
        return SimpleNamespace(new_sign_count=1)

    def test_idless_options_request_discoverable_credential_and_uv(self):
        self.payload = {"discoverable": True}
        result = self.ns["auth_options"]()
        self.assertNotIn("allowCredentials", result)
        self.assertEqual(result["userVerification"], "required")
        self.assertEqual(self.session["webauthn_auth_discoverable_username"], "")

    def test_idless_login_identifies_non_admin_only_after_credential_lookup(self):
        self.payload = {"discoverable": True}
        self.ns["auth_options"]()
        self.payload = {"discoverable": True, "credential": {"id": "credential-1"}}
        result = self.ns["auth_verify"]()
        self.assertTrue(result["ok"])
        self.assertEqual(self.session["user"], "alice")
        self.assertTrue(self.verify_calls[0]["require_user_verification"])
        self.assertEqual(len(self.login_logs), 1)
        self.assertNotIn("webauthn_auth_discoverable_challenge", self.session)

    def test_admin_can_log_in_directly_with_discoverable_passkey_and_uv(self):
        self.rows[0]["username"] = "admin"
        self.payload = {"discoverable": True}
        self.ns["auth_options"]()
        self.payload = {"discoverable": True, "credential": {"id": "credential-1"}}
        result = self.ns["auth_verify"]()
        self.assertTrue(result["ok"])
        self.assertTrue(self.verify_calls[0]["require_user_verification"])
        self.assertEqual(self.admin_sessions[0]["method"], "passkey_direct")

    def test_named_admin_passkey_still_requires_password_preauth(self):
        self.payload = {"username": "admin"}
        result, status = self.ns["auth_options"]()
        self.assertEqual(status, 401)
        self.assertIn("error", result)

    def test_duplicate_credential_id_cannot_choose_an_account(self):
        self.rows.append({**self.rows[0], "id": 2, "username": "bob"})
        self.payload = {"discoverable": True}
        self.ns["auth_options"]()
        self.payload = {"discoverable": True, "credential": {"id": "credential-1"}}
        self.assertEqual(self.ns["auth_verify"]()[1], 404)
        self.assertFalse(self.verify_calls)
        self.assertNotIn("user", self.session)

    def test_idless_login_requires_its_own_challenge(self):
        self.payload = {"discoverable": True, "credential": {"id": "credential-1"}}
        self.assertEqual(self.ns["auth_verify"]()[1], 400)
        self.assertFalse(self.verify_calls)

    def test_idless_flow_rejects_username_or_admin_purpose(self):
        self.payload = {"discoverable": True, "username": "admin"}
        self.assertEqual(self.ns["auth_options"]()[1], 400)
        self.payload = {"discoverable": True, "purpose": "admin_action"}
        self.assertEqual(self.ns["auth_options"]()[1], 400)

    def test_all_new_passkeys_require_discoverable_credential_and_uv(self):
        self.assertIn('authenticator_selection["residentKey"] = "required"', SOURCE)
        self.assertIn('authenticator_selection["userVerification"] = "required"', SOURCE)


if __name__ == "__main__":
    unittest.main()
