import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Blueprint, Flask


class FakePushDispatchError(Exception):
    def __init__(self, reason: str, *, status_code: int = 400, detail: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.detail = detail


class InternalPushApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = Path(__file__).resolve().parents[1]

        app_module = types.ModuleType("app")
        app_module.__path__ = [str(repo_root)]  # type: ignore[attr-defined]
        sys.modules["app"] = app_module

        ext_pkg = types.ModuleType("app.external_login_user")
        ext_pkg.__path__ = [str(repo_root / "external_login_user")]  # type: ignore[attr-defined]
        ext_pkg.bp = Blueprint("external_login_user", __name__)
        sys.modules["app.external_login_user"] = ext_pkg

        ext_utils_module = types.ModuleType("app.external_login_user.utils")
        ext_utils_module._require_ext_login = lambda: None
        sys.modules["app.external_login_user.utils"] = ext_utils_module

        app_utils_module = types.ModuleType("app.utils")
        app_utils_module.__path__ = [str(repo_root / "utils")]  # type: ignore[attr-defined]
        sys.modules["app.utils"] = app_utils_module

        db_module = types.ModuleType("app.utils.db")
        db_module.get_db = lambda: None
        sys.modules["app.utils.db"] = db_module

        push_module = types.ModuleType("app.utils.push")
        push_module.PushDispatchError = FakePushDispatchError
        push_module.send_push = lambda **kwargs: {"ok": True}
        sys.modules["app.utils.push"] = push_module
        cls.push_module = push_module

        notif_spec = importlib.util.spec_from_file_location(
            "app.external_login_user.notifications",
            repo_root / "external_login_user" / "notifications.py",
        )
        notif_module = importlib.util.module_from_spec(notif_spec)
        assert notif_spec and notif_spec.loader
        sys.modules["app.external_login_user.notifications"] = notif_module
        notif_spec.loader.exec_module(notif_module)
        cls.notifications = notif_module

    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(self.notifications.mfu_notifications_bp)
        self.client = self.app.test_client()

    def test_internal_push_requires_api_key(self):
        with patch.dict(os.environ, {"MFU_INTERNAL_API_KEY": "secret"}, clear=False):
            res = self.client.post("/api/internal/push/send", json={})
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.get_json()["reason"], "missing_internal_key")

    def test_internal_push_create_in_app_only(self):
        captured = {}

        def fake_send_push(**kwargs):
            captured.update(kwargs)
            return {
                "ok": True,
                "created": True,
                "duplicate": False,
                "notification_id": 101,
                "delivery": {"in_app": "created", "web_push": "skipped"},
            }

        with (
            patch.dict(os.environ, {"MFU_INTERNAL_API_KEY": "secret"}, clear=False),
            patch.object(self.push_module, "send_push", side_effect=fake_send_push),
        ):
            res = self.client.post(
                "/api/internal/push/send",
                headers={"X-MFU-Internal-Key": "secret"},
                json={
                    "recipient_type": "external_user_id",
                    "recipient_value": 123,
                    "title": "写真アップロード完了",
                    "body": "アルバムに新しい写真が追加されました。",
                    "target_url": "/albums/123",
                    "dedup_key": "album:123:upload_complete:user:123",
                    "create_in_app": True,
                    "send_web_push": False,
                },
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(captured["recipient_type"], "external_user_id")
        self.assertFalse(captured["send_web_push"])
        self.assertEqual(res.get_json()["delivery"]["in_app"], "created")

    def test_internal_push_web_push_result_passthrough(self):
        with (
            patch.dict(os.environ, {"MFU_INTERNAL_API_KEY": "secret"}, clear=False),
            patch.object(
                self.push_module,
                "send_push",
                return_value={
                    "ok": True,
                    "created": True,
                    "duplicate": False,
                    "notification_id": 202,
                    "delivery": {"in_app": "created", "web_push": "sent"},
                },
            ),
        ):
            res = self.client.post(
                "/api/internal/push/send",
                headers={"X-MFU-Internal-Key": "secret"},
                json={
                    "recipient_type": "mfu_username",
                    "recipient_value": "admin",
                    "title": "管理通知",
                    "body": "Pushあり",
                    "target_url": "/mfu-notifications",
                    "dedup_key": "admin:test:webpush",
                    "create_in_app": True,
                    "send_web_push": True,
                },
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["delivery"]["web_push"], "sent")

    def test_internal_push_duplicate_passthrough(self):
        with (
            patch.dict(os.environ, {"MFU_INTERNAL_API_KEY": "secret"}, clear=False),
            patch.object(
                self.push_module,
                "send_push",
                return_value={
                    "ok": True,
                    "created": False,
                    "duplicate": True,
                    "notification_id": None,
                    "delivery": {"in_app": "duplicate", "web_push": "skipped"},
                },
            ),
        ):
            res = self.client.post(
                "/api/internal/push/send",
                headers={"X-MFU-Internal-Key": "secret"},
                json={
                    "recipient_type": "external_user_id",
                    "recipient_value": 123,
                    "title": "重複通知",
                    "body": "same",
                    "target_url": "/albums/123",
                    "dedup_key": "duplicate:key",
                },
            )
        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertTrue(payload["duplicate"])
        self.assertEqual(payload["delivery"]["in_app"], "duplicate")


if __name__ == "__main__":
    unittest.main()
