import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def exception(self, *args, **kwargs):
        return None


class _DummyBlueprint:
    def __init__(self, *args, **kwargs):
        return None

    def app_context_processor(self, func):
        return func

    def record_once(self, func):
        return func

    def get(self, *args, **kwargs):
        return lambda func: func

    def post(self, *args, **kwargs):
        return lambda func: func


def _install_flask_stub():
    flask_module = types.ModuleType("flask")
    flask_module.Blueprint = _DummyBlueprint
    flask_module.abort = lambda *args, **kwargs: None
    flask_module.current_app = types.SimpleNamespace(logger=_DummyLogger())
    flask_module.jsonify = lambda payload=None, *args, **kwargs: payload
    flask_module.redirect = lambda value, *args, **kwargs: value
    flask_module.render_template = lambda *args, **kwargs: ""
    flask_module.request = types.SimpleNamespace(args={}, view_args={}, endpoint=None)
    flask_module.session = {}
    sys.modules["flask"] = flask_module


def load_mail_module():
    repo_root = Path(__file__).resolve().parents[1]
    app_module = types.ModuleType("app")
    app_module.__path__ = []  # type: ignore[attr-defined]
    app_utils_module = types.ModuleType("app.utils")
    app_utils_module.__path__ = []  # type: ignore[attr-defined]
    app_utils_logs = types.ModuleType("app.utils.logs")
    app_utils_logs.write_smtp_log = lambda *args, **kwargs: None
    app_utils_mail_delivery = types.ModuleType("app.utils.mail_delivery")
    app_utils_mail_delivery.generate_message_id = lambda: ("uuid", "message@example.com")
    app_utils_mail_delivery.record_mail_submission = lambda *args, **kwargs: None
    sys.modules["app"] = app_module
    sys.modules["app.utils"] = app_utils_module
    sys.modules["app.utils.logs"] = app_utils_logs
    sys.modules["app.utils.mail_delivery"] = app_utils_mail_delivery

    spec = importlib.util.spec_from_file_location("app.utils.mail", repo_root / "utils" / "mail.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["app.utils.mail"] = module
    spec.loader.exec_module(module)
    return module


def load_notifications_module():
    repo_root = Path(__file__).resolve().parents[1]
    _install_flask_stub()

    package_module = types.ModuleType("external_login_user")
    package_module.__path__ = [str(repo_root / "external_login_user")]  # type: ignore[attr-defined]
    package_module.bp = _DummyBlueprint()
    sys.modules["external_login_user"] = package_module

    ext_utils_module = types.ModuleType("external_login_user.utils")
    ext_utils_module._require_ext_login = lambda: None
    sys.modules["external_login_user.utils"] = ext_utils_module

    app_module = types.ModuleType("app")
    app_module.__path__ = []  # type: ignore[attr-defined]
    app_utils_module = types.ModuleType("app.utils")
    app_utils_module.__path__ = []  # type: ignore[attr-defined]
    app_utils_db = types.ModuleType("app.utils.db")
    app_utils_db.get_db = lambda: None
    app_utils_mail = types.ModuleType("app.utils.mail")
    app_utils_mail.send_external_unread_reminder_mail = lambda *args, **kwargs: None
    app_chat = types.ModuleType("app.chat")
    app_chat.__path__ = []  # type: ignore[attr-defined]
    app_chat_socket = types.ModuleType("app.chat.socketio_ext")
    app_chat_socket.socketio = None
    sys.modules["app"] = app_module
    sys.modules["app.utils"] = app_utils_module
    sys.modules["app.utils.db"] = app_utils_db
    sys.modules["app.utils.mail"] = app_utils_mail
    sys.modules["app.chat"] = app_chat
    sys.modules["app.chat.socketio_ext"] = app_chat_socket

    spec = importlib.util.spec_from_file_location(
        "external_login_user.notifications",
        repo_root / "external_login_user" / "notifications.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["external_login_user.notifications"] = module
    spec.loader.exec_module(module)
    return module


class ExternalUnreadReminderMailTest(unittest.TestCase):
    def test_send_external_unread_reminder_mail_uses_existing_send_mail(self):
        mail_module = load_mail_module()

        with patch.object(mail_module, "send_mail") as mocked:
            mail_module.send_external_unread_reminder_mail(
                "user@example.com",
                external_login_user_id=123,
            )

        mocked.assert_called_once_with(
            "user@example.com",
            mail_module.EXTERNAL_UNREAD_REMINDER_SUBJECT,
            mail_module.EXTERNAL_UNREAD_REMINDER_BODY,
            external_login_user_id=123,
            mail_kind="external_unread_reminder",
            append_signature=False,
        )


class ExternalUnreadReminderNotificationTest(unittest.TestCase):
    def test_same_jst_day_uses_jst_boundary(self):
        notifications = load_notifications_module()
        late_jst = datetime(2026, 3, 19, 14, 59, tzinfo=timezone.utc)
        next_day_jst = datetime(2026, 3, 19, 15, 1, tzinfo=timezone.utc)
        same_day_jst = datetime(2026, 3, 19, 3, 0, tzinfo=timezone.utc)

        self.assertFalse(notifications._same_jst_day(late_jst, next_day_jst))
        self.assertTrue(notifications._same_jst_day(late_jst, same_day_jst))

    def test_send_external_unread_reminder_emails_skips_user_already_sent_today(self):
        notifications = load_notifications_module()

        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows

            def execute(self, *args, **kwargs):
                return None

            def fetchall(self):
                return self._rows

            def close(self):
                return None

        class FakeDB:
            def __init__(self, rows):
                self._rows = rows

            def cursor(self, dictionary=True):
                return FakeCursor(self._rows)

            def close(self):
                return None

        last_sent_at = datetime(2026, 3, 20, 1, 0, 0)
        rows = [{"id": 10, "email": "user@example.com", "last_sent_at": last_sent_at}]

        with (
            patch.object(notifications, "_ensure_notification_schema"),
            patch.object(notifications, "get_db", return_value=FakeDB(rows)),
            patch.object(notifications, "send_external_unread_reminder_mail") as mocked_send,
            patch.object(notifications, "_compute_unread_count_external") as mocked_unread,
        ):
            summary = notifications.send_external_unread_reminder_emails(
                now_utc=datetime(2026, 3, 20, 2, 0, tzinfo=timezone.utc)
            )

        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["skipped_already_sent_today"], 1)
        self.assertEqual(summary["sent"], 0)
        mocked_send.assert_not_called()
        mocked_unread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
