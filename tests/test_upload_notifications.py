import importlib.util
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "testable_upload_notifications",
    ROOT / "utils" / "upload_notifications.py",
)
assert SPEC and SPEC.loader
notifications = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notifications)


class UploadNotificationTest(unittest.TestCase):
    def setUp(self):
        self.posted_at = datetime(2026, 8, 8, 22, 8, 44)
        self.detail_url = "https://mfu.iori0624.jp/layer_upload_list/abc123"

    def test_mail_message_uses_detail_page_without_zip_download_wording(self):
        message = notifications.build_processed_upload_message(
            title="撮影テスト",
            comment="確認をお願いします",
            detail_url=self.detail_url,
            image_count=12,
            posted_at=self.posted_at,
        )

        self.assertIn("画像枚数: 12枚", message)
        self.assertIn(f"詳細を確認:\n{self.detail_url}", message)
        self.assertNotIn("ダウンロード:", message)
        self.assertNotIn(".zip", message)

    def test_discord_embed_is_clickable_blue_card(self):
        embed = notifications.build_processed_upload_discord_embed(
            title="撮影テスト",
            comment="確認をお願いします",
            detail_url=self.detail_url,
            image_count=12,
            posted_at=self.posted_at,
        )

        self.assertEqual(embed["url"], self.detail_url)
        self.assertEqual(embed["color"], 0x3498DB)
        self.assertEqual(embed["title"], "📸 加工済み写真がアップロードされました")
        self.assertTrue(any(field["name"] == "コメント" for field in embed["fields"]))

    def test_discord_sender_posts_embed_payload_without_mentions(self):
        response = Mock(status_code=204, text="")
        response.raise_for_status.return_value = None
        embed = {"title": "test", "url": self.detail_url}
        logger = Mock()

        with patch.object(notifications.requests, "post", return_value=response) as post:
            sent = notifications.send_discord_upload_notification(
                logger=logger,
                username="admin",
                notify_method="discord",
                webhook_url="https://discord.example/webhook",
                upload_id="reply1",
                message="fallback",
                context_label="layer upload",
                embed=embed,
            )

        self.assertTrue(sent)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["embeds"], [embed])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("content", payload)


if __name__ == "__main__":
    unittest.main()
