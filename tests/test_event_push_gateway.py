import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class EventPushGatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = Path(__file__).resolve().parents[1]
        db_module = types.ModuleType("app.utils.db")
        db_module.get_db = lambda: None
        with patch.dict(sys.modules, {"app.utils.db": db_module}):
            spec = importlib.util.spec_from_file_location(
                "event_push_gateway_under_test", repo_root / "utils" / "push.py"
            )
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        cls.module = module

    def test_event_push_creates_in_app_and_web_push(self):
        with patch.object(
            self.module,
            "send_push",
            return_value={"ok": True, "created": True},
        ) as sender:
            result = self.module.send_external_event_push(
                user_id=12,
                event_id=34,
                event_uuid="11111111-1111-1111-1111-111111111111",
                kind="event_payment_status",
                title="更新",
                body="支払状態が更新されました。",
                target_suffix="payment",
                dedup_token="test-token",
            )

        self.assertTrue(result["ok"])
        args = sender.call_args.kwargs
        self.assertEqual(args["recipient_type"], "external_user_id")
        self.assertEqual(args["recipient_value"], 12)
        self.assertEqual(
            args["target_url"],
            "/external-login/app/events/11111111-1111-1111-1111-111111111111/payment",
        )
        self.assertTrue(args["create_in_app"])
        self.assertTrue(args["send_web_push"])
        self.assertEqual(args["dedup_key"], "event:34:event_payment_status:12:test-token")

    def test_explicit_album_target_does_not_require_event_uuid(self):
        with patch.object(
            self.module,
            "send_push",
            return_value={"ok": True, "created": True},
        ) as sender:
            result = self.module.send_external_event_push(
                user_id=12,
                event_id=34,
                event_uuid="",
                kind="album_process_request",
                title="加工依頼",
                body="加工をお願いします。",
                target_url="/external-login/app/albums/a/children/b",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            sender.call_args.kwargs["target_url"],
            "/external-login/app/albums/a/children/b",
        )

    def test_delivery_failure_does_not_escape_business_operation(self):
        with patch.object(self.module, "send_push", side_effect=RuntimeError("offline")):
            result = self.module.send_external_event_push(
                user_id=12,
                event_id=34,
                event_uuid="11111111-1111-1111-1111-111111111111",
                kind="event_join_pending",
                title="申請受付",
                body="承認待ちです。",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
