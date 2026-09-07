import importlib.util
import base64
import sys
import tempfile
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

    def test_unregistered_payload_is_compact_embed_with_four_buttons(self):
        payload = MODULE.build_discord_payload(
            "08093242655",
            classification="unregistered",
            caller_name="未登録",
            did="05068741025",
            process="voicemail_announce",
            blacklist_url="https://mfu.example/black#token",
            whitelist_url="https://mfu.example/white#token",
            click_to_call_url="https://mfu.example/call#token",
            now=datetime(2026, 7, 28, 12, 34, 56, tzinfo=ZoneInfo("Asia/Tokyo")),
        )
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "📞 ホワイトリスト外からの着信")
        self.assertEqual(
            [field["name"] for field in embed["fields"]],
            ["日時", "相手", "名称", "着信先　｜　処理"],
        )
        self.assertFalse(embed["fields"][3]["inline"])
        self.assertEqual(
            embed["fields"][3]["value"],
            "05068741025　｜　VoiceMail_Announceへ転送",
        )
        buttons = payload["components"][0]["components"]
        self.assertEqual(len(buttons), 4)
        self.assertTrue(all(button["style"] == 5 for button in buttons))
        self.assertEqual(
            [button["label"] for button in buttons],
            ["📞 折り返し発信", "✅ ホワイトリストへ登録", "🚫 ブラックリストへ登録", "📖 電話帳ナビ"],
        )

    def test_whitelist_has_only_callback_and_phonebook_buttons(self):
        payload = MODULE.build_discord_payload(
            "08093242655",
            classification="whitelist",
            caller_name="自分の電話番号",
            click_to_call_url="https://mfu.example/call#token",
        )
        buttons = payload["components"][0]["components"]
        self.assertEqual([button["label"] for button in buttons], ["📞 折り返し発信", "📖 電話帳ナビ"])

    def test_no_buttons_for_anonymous_or_blacklist(self):
        payload = MODULE.build_discord_payload("", classification="anonymous", process="hangup_21")
        self.assertNotIn("components", payload)
        payload = MODULE.build_discord_payload("08093242655", classification="blacklist", process="hangup_21")
        self.assertNotIn("components", payload)

    def test_notification_name_metadata_wins_over_truncated_sip_name(self):
        full_name = "ベリーベスト　錦糸町オフィス"
        sip_name = "ベリーベスト　錦糸町オフィ"
        encode = lambda value: base64.b64encode(value.encode("utf-8")).decode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "caller_whitelist.txt"
            path.write_text(
                f"# MFU_NOTIFY_NAME|W|0358754931|{encode(full_name)}\n"
                f"0358754931|{encode(sip_name)}\n",
                encoding="utf-8",
            )
            whitelist, blacklist = MODULE.load_phone_names(path)
        self.assertEqual(whitelist["0358754931"], full_name)
        self.assertEqual(blacklist, {})


if __name__ == "__main__":
    unittest.main()
