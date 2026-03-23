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
    class _FakeCursor:
        def __init__(self, rows=None):
            self._rows = rows or []
            self.executed = []

        def execute(self, query, params=None):
            self.executed.append((query, params))
            return None

        def fetchall(self):
            return self._rows

        def close(self):
            return None

    class _FakeDB:
        def __init__(self, rows=None):
            self._rows = rows or []
            self.select_cursor = ExternalUnreadReminderNotificationTest._FakeCursor(self._rows)
            self.update_cursor = ExternalUnreadReminderNotificationTest._FakeCursor()
            self.committed = False
            self.rolled_back = False

        def cursor(self, dictionary=False):
            return self.select_cursor if dictionary else self.update_cursor

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            return None

    def test_can_send_external_unread_reminder_allows_null_last_sent_at(self):
        notifications = load_notifications_module()

        self.assertTrue(
            notifications._can_send_external_unread_reminder(
                None,
                datetime(2026, 3, 22, 10, 0, tzinfo=timezone.utc),
            )
        )

    def test_send_external_unread_reminder_emails_skips_no_unread_before_too_soon(self):
        notifications = load_notifications_module()
        rows = [{"id": 10, "email": "user@example.com", "last_sent_at": datetime(2026, 3, 22, 9, 0, 0)}]
        select_db = self._FakeDB(rows)
        update_db = self._FakeDB()

        with (
            patch.object(notifications, "_ensure_notification_schema"),
            patch.object(notifications, "get_db", side_effect=[select_db, update_db]),
            patch.object(notifications, "send_external_unread_reminder_mail") as mocked_send,
            patch.object(notifications, "_compute_unread_count_external", return_value=0) as mocked_unread,
        ):
            summary = notifications.send_external_unread_reminder_emails(
                now_utc=datetime(2026, 3, 22, 9, 59, 59, tzinfo=timezone.utc)
            )

        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["skipped_no_unread"], 1)
        self.assertEqual(summary["skipped_too_soon"], 0)
        self.assertEqual(summary["sent"], 0)
        mocked_send.assert_not_called()
        mocked_unread.assert_called_once_with(10)
        self.assertEqual(update_db.update_cursor.executed, [])

    def test_send_external_unread_reminder_emails_skips_user_when_last_send_is_less_than_48_hours(self):
        notifications = load_notifications_module()
        rows = [{"id": 10, "email": "user@example.com", "last_sent_at": datetime(2026, 3, 20, 10, 0, 0)}]
        select_db = self._FakeDB(rows)
        update_db = self._FakeDB()

        with (
            patch.object(notifications, "_ensure_notification_schema"),
            patch.object(notifications, "get_db", side_effect=[select_db, update_db]),
            patch.object(notifications, "send_external_unread_reminder_mail") as mocked_send,
            patch.object(notifications, "_compute_unread_count_external", return_value=2) as mocked_unread,
        ):
            summary = notifications.send_external_unread_reminder_emails(
                now_utc=datetime(2026, 3, 22, 9, 59, 59, tzinfo=timezone.utc)
            )

        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["skipped_too_soon"], 1)
        self.assertEqual(summary["sent"], 0)
        mocked_send.assert_not_called()
        mocked_unread.assert_called_once_with(10)
        self.assertEqual(update_db.update_cursor.executed, [])

    def test_send_external_unread_reminder_emails_sends_when_last_send_is_exactly_48_hours_ago(self):
        notifications = load_notifications_module()
        rows = [{"id": 10, "email": "user@example.com", "last_sent_at": datetime(2026, 3, 20, 10, 0, 0)}]
        select_db = self._FakeDB(rows)
        update_db = self._FakeDB()
        get_db_side_effect = [select_db, update_db]
        now_utc = datetime(2026, 3, 22, 10, 0, tzinfo=timezone.utc)

        with (
            patch.object(notifications, "_ensure_notification_schema"),
            patch.object(notifications, "get_db", side_effect=get_db_side_effect),
            patch.object(notifications, "send_external_unread_reminder_mail") as mocked_send,
            patch.object(notifications, "_compute_unread_count_external", return_value=3),
        ):
            summary = notifications.send_external_unread_reminder_emails(now_utc=now_utc)

        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["failed"], 0)
        mocked_send.assert_called_once_with("user@example.com", external_login_user_id=10)
        self.assertTrue(update_db.committed)
        self.assertFalse(update_db.rolled_back)
        self.assertEqual(len(update_db.update_cursor.executed), 1)
        executed_query, executed_params = update_db.update_cursor.executed[0]
        self.assertEqual(
            " ".join(executed_query.split()),
            "UPDATE external_login_user SET notification_unread_reminder_last_sent_at=%s WHERE id=%s",
        )
        self.assertEqual(executed_params, (now_utc.replace(tzinfo=None), 10))

    def test_send_external_unread_reminder_emails_updates_last_sent_at_only_on_success(self):
        notifications = load_notifications_module()
        rows = [{"id": 10, "email": "user@example.com", "last_sent_at": None}]
        select_db = self._FakeDB(rows)
        update_db = self._FakeDB()
        get_db_side_effect = [select_db, update_db]
        now_utc = datetime(2026, 3, 22, 10, 0, tzinfo=timezone.utc)

        with (
            patch.object(notifications, "_ensure_notification_schema"),
            patch.object(notifications, "get_db", side_effect=get_db_side_effect),
            patch.object(notifications, "send_external_unread_reminder_mail") as mocked_send,
            patch.object(notifications, "_compute_unread_count_external", return_value=1),
        ):
            summary = notifications.send_external_unread_reminder_emails(now_utc=now_utc)

        self.assertEqual(summary["sent"], 1)
        mocked_send.assert_called_once_with("user@example.com", external_login_user_id=10)
        self.assertTrue(update_db.committed)
        self.assertEqual(len(update_db.update_cursor.executed), 1)

    def test_send_external_unread_reminder_emails_does_not_update_last_sent_at_on_failure(self):
        notifications = load_notifications_module()
        rows = [{"id": 10, "email": "user@example.com", "last_sent_at": None}]
        select_db = self._FakeDB(rows)
        update_db = self._FakeDB()

        with (
            patch.object(notifications, "_ensure_notification_schema"),
            patch.object(notifications, "get_db", side_effect=[select_db, update_db]),
            patch.object(
                notifications,
                "send_external_unread_reminder_mail",
                side_effect=RuntimeError("smtp error"),
            ) as mocked_send,
            patch.object(notifications, "_compute_unread_count_external", return_value=2),
        ):
            summary = notifications.send_external_unread_reminder_emails(
                now_utc=datetime(2026, 3, 22, 10, 0, tzinfo=timezone.utc)
            )

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["sent"], 0)
        mocked_send.assert_called_once_with("user@example.com", external_login_user_id=10)
        self.assertFalse(update_db.committed)
        self.assertEqual(update_db.update_cursor.executed, [])


if __name__ == "__main__":
    unittest.main()
