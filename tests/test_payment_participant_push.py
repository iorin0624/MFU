import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.params = None

    def execute(self, _sql, params):
        self.params = params

    def fetchone(self):
        return self.row

    def close(self):
        pass


class _Connection:
    def __init__(self, row):
        self.cursor_instance = _Cursor(row)

    def cursor(self, dictionary=False):
        assert dictionary
        return self.cursor_instance

    def close(self):
        pass


class PaymentParticipantPushTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = Path(__file__).resolve().parents[1]
        cls.sender = Mock(return_value={"ok": True})
        cls.connection = _Connection(
            {"title": "テストイベント", "event_uuid": "event-uuid", "nickname": "Aさん"}
        )

        app_module = types.ModuleType("app")
        app_module.__path__ = []
        ext_module = types.ModuleType("app.external_login_user")
        ext_module.__path__ = []
        utils_package = types.ModuleType("app.utils")
        utils_package.__path__ = []
        db_module = types.ModuleType("app.utils.db")
        db_module.get_db = lambda: cls.connection
        push_module = types.ModuleType("app.utils.push")
        push_module.send_external_event_push = cls.sender
        ext_utils_module = types.ModuleType("app.external_login_user.utils")
        ext_utils_module._uuid_bytes_to_str = lambda value: value

        modules = {
            "app": app_module,
            "app.external_login_user": ext_module,
            "app.external_login_user.utils": ext_utils_module,
            "app.utils": utils_package,
            "app.utils.db": db_module,
            "app.utils.push": push_module,
        }
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location(
                "app.external_login_user.event_push",
                repo_root / "external_login_user" / "event_push.py",
            )
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        cls.module = module

    def setUp(self):
        self.sender.reset_mock()

    def test_payment_notification_forwards_custom_title_and_dedup_token(self):
        result = self.module.notify_member_payment_push(
            event_id=10,
            user_id=20,
            payment_status="paid",
            kind="event_tip_completed",
            title_suffix="ご支援ありがとうございます💕",
            body="お礼",
            dedup_token="tip:abc",
        )

        self.assertTrue(result["ok"])
        args = self.sender.call_args.kwargs
        self.assertEqual(args["title"], "【テストイベント】ご支援ありがとうございます💕")
        self.assertEqual(args["dedup_token"], "tip:abc")
        self.assertEqual(args["target_suffix"], "/payment")
        self.assertEqual(self.connection.cursor_instance.params, (20, 10))

    def test_square_and_refund_hooks_are_present_in_common_completion_paths(self):
        repo_root = Path(__file__).resolve().parents[1]
        payment_source = (repo_root / "payment" / "__init__.py").read_text(encoding="utf-8")
        browser_source = (repo_root / "external_login_user" / "payments.py").read_text(encoding="utf-8")

        self.assertIn('kind="event_payment_square_completed"', payment_source)
        self.assertIn('kind="event_tip_completed"', payment_source)
        self.assertIn('kind="event_payment_refund_completed"', payment_source)
        self.assertIn("push_result = _send_refund_completion_push", payment_source)
        self.assertIn('dedup_token=f"square:{token or resolved_payment_row_id', browser_source)


if __name__ == "__main__":
    unittest.main()
