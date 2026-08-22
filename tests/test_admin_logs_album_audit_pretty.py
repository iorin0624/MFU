import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AdminLogsAlbumAuditPrettyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "admin_logs.html").read_text(
            encoding="utf-8-sig"
        )

    def test_event_album_preview_block_has_dedicated_pretty_view(self):
        self.assertIn(r"/\[EVENT_ALBUM_PREVIEW_BLOCKED\]/", self.template)
        self.assertIn("プレビュー拒否", self.template)
        self.assertIn("アルバム未表示", self.template)

    def test_inapp_warning_identifies_the_200_as_a_warning_page(self):
        self.assertIn(r"/\[INAPP_WARNING\]/", self.template)
        self.assertIn("アプリ内ブラウザ案内", self.template)
        self.assertIn("HTTP ' + status + '（案内画面）", self.template)

    def test_granted_album_view_shows_event_auth_and_user(self):
        self.assertIn(r"/\[ALBUM_VIEW_GRANTED\]/", self.template)
        self.assertIn("アルバム閲覧許可", self.template)
        self.assertIn("イベントID ", self.template)
        self.assertIn("event_session: 'イベント認証'", self.template)
        self.assertIn(r't.match(/\buser="([^"]*)"/i)', self.template)

    def test_blocked_and_additional_login_types_have_pretty_views(self):
        for marker in (
            "LINE_LOGIN_BLOCKED",
            "LOGIN_PASSKEY",
            "PIN_LOGIN",
            "LOGIN_EMAIL_OTP",
            "LOGIN_TOTP",
        ):
            self.assertIn(marker, self.template)
        self.assertIn("LINEログイン拒否", self.template)
        self.assertIn("退会済みアカウント", self.template)
        self.assertIn("パスキー認証", self.template)
        self.assertIn("メールOTP認証", self.template)

    def test_phone_audit_json_logs_have_pretty_views(self):
        self.assertIn("parseJsonAudit(t, 'PHONE_WHITELIST')", self.template)
        self.assertIn("parseJsonAudit(t, 'PHONE_DIAGNOSTICS')", self.template)
        self.assertIn("ホワイトリスト追加", self.template)
        self.assertIn("ブラックリスト追加", self.template)
        self.assertIn("電話診断", self.template)
        self.assertIn("登録件数 ", self.template)

    def test_other_current_tagged_logs_have_pretty_views(self):
        self.assertIn(r"/^\[SUCAL\]/", self.template)
        self.assertIn(r"/^\[ALERT\]/", self.template)
        self.assertIn(r"/^\[UA\]/", self.template)
        self.assertIn("カレンダー", self.template)
        self.assertIn("セキュリティ警告", self.template)
        self.assertIn("UA記録", self.template)

    def test_unknown_tagged_logs_never_become_blank(self):
        self.assertIn("const genericBracketTag =", self.template)
        self.assertIn("const genericNamedTag =", self.template)
        self.assertIn("未知のタグ付きログも、見やすい表示で空欄にしない", self.template)


if __name__ == "__main__":
    unittest.main()
