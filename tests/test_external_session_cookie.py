import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


def load_session_cookie_module():
    repo_root = Path(__file__).resolve().parents[1]
    flask_module = types.ModuleType("flask")
    flask_sessions_module = types.ModuleType("flask.sessions")

    class _SecureCookieSessionInterface:
        def get_expiration_time(self, app, session):
            return None

    flask_sessions_module.SecureCookieSessionInterface = _SecureCookieSessionInterface
    sys.modules["flask"] = flask_module
    sys.modules["flask.sessions"] = flask_sessions_module
    spec = importlib.util.spec_from_file_location(
        "mfu_admin_session_cookie_test",
        repo_root / "utils" / "admin_session_cookie.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _Session(dict):
    permanent = True


class ExternalSessionCookieTest(unittest.TestCase):
    def test_external_user_cookie_has_rolling_seven_day_expiry(self):
        module = load_session_cookie_module()
        interface = module.MFUSecureCookieSessionInterface()
        before = datetime.now(timezone.utc) + timedelta(days=7)

        expires_at = interface.get_expiration_time(None, _Session(ext_user_id=123))

        after = datetime.now(timezone.utc) + timedelta(days=7)
        self.assertGreaterEqual(expires_at, before)
        self.assertLessEqual(expires_at, after)

    def test_pin_login_marks_external_session_permanent(self):
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / "external_login_user" / "users.py").read_text(encoding="utf-8")
        pin_login = source[source.index("def pin_login():"):source.index("def event_album_direct")]

        self.assertIn('session["ext_user_id"] = target["id"]', pin_login)
        self.assertIn("session.permanent = True", pin_login)

    def test_existing_external_session_is_upgraded_and_refreshed_on_access(self):
        repo_root = Path(__file__).resolve().parents[1]
        source = (repo_root / "external_login_user" / "__init__.py").read_text(encoding="utf-8")
        refresh = source[
            source.index("def _refresh_external_user_session_lifetime"):
            source.index("def _lock_deleted_external_user")
        ]

        self.assertIn('session.get("ext_user_id")', refresh)
        self.assertIn("session.permanent = True", refresh)


if __name__ == "__main__":
    unittest.main()
