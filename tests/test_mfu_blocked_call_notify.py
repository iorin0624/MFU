import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


MODULE_PATH = Path(__file__).resolve().parents[1] / "deploy" / "mfu_blocked_call_notify.py"
SPEC = importlib.util.spec_from_file_location("mfu_blocked_call_notify", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BlockedCallNotifyTest(unittest.TestCase):
    SECRET = "11" * 32

    def test_registration_urls_share_one_token(self):
        blacklist_url, whitelist_url, click_to_call_url = MODULE.build_registration_urls(
            "08093242655",
            self.SECRET,
            "https://mfu.example/phone-blacklist/register",
            "https://mfu.example/phone-whitelist/register",
            now=1000,
        )
        self.assertEqual(blacklist_url.split("#", 1)[1], whitelist_url.split("#", 1)[1])
        self.assertNotEqual(blacklist_url.split("#", 1)[1], click_to_call_url.split("#", 1)[1])

    def test_payload_uses_three_link_buttons_without_raw_urls(self):
        payload = MODULE.build_discord_payload(
            "08093242655",
            "https://mfu.example/black#token",
            "https://mfu.example/white#token",
            "https://mfu.example/call#token",
            now=datetime(2026, 7, 28, 12, 34, 56, tzinfo=ZoneInfo("Asia/Tokyo")),
        )
        self.assertNotIn("https://", payload["content"])
        buttons = payload["components"][0]["components"]
        self.assertEqual(len(buttons), 4)
        self.assertTrue(all(button["style"] == 5 for button in buttons))
        self.assertEqual(
            [button["label"] for button in buttons],
            ["📖 電話帳ナビ", "📞 折り返し発信", "✅ ホワイトリストへ登録", "🚫 ブラックリストへ登録"],
        )

    def test_no_buttons_for_anonymous_caller(self):
        payload = MODULE.build_discord_payload("")
        self.assertNotIn("components", payload)


if __name__ == "__main__":
    unittest.main()
