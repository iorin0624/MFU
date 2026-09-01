from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ExternalTestAccountTests(unittest.TestCase):
    def test_schema_has_test_account_state(self):
        source = (ROOT / "external_login_user" / "schema.py").read_text(encoding="utf-8")
        self.assertIn("is_test_account", source)
        self.assertIn("test_account_enabled", source)
        self.assertIn("last_login_at", source)

    def test_admin_can_issue_global_email_pin_account(self):
        source = (ROOT / "external_login_user" / "admin.py").read_text(encoding="utf-8")
        self.assertIn('/admin/test-accounts', source)
        self.assertIn('social_id = "email_test:"', source)
        self.assertIn("is_test_account, test_account_enabled", source)
        creation = source[source.index("def admin_test_accounts"):source.index("def admin_event_test_accounts")]
        self.assertNotIn("INSERT INTO mfu_event_member", creation)

    def test_disabled_test_account_cannot_login(self):
        users = (ROOT / "external_login_user" / "users.py").read_text(encoding="utf-8")
        utils = (ROOT / "external_login_user" / "utils.py").read_text(encoding="utf-8")
        condition = "COALESCE(is_test_account, 0)=0 OR COALESCE(test_account_enabled, 1)=1"
        self.assertIn(condition, users)
        self.assertIn(condition, utils)

    def test_event_list_links_to_test_account_management(self):
        template = (ROOT / "external_login_user" / "template" / "admin_events_list.html").read_text(encoding="utf-8")
        self.assertIn("admin_test_accounts", template)
        self.assertIn("テストアカウント管理", template)

    def test_global_account_can_be_assigned_to_multiple_events(self):
        source = (ROOT / "external_login_user" / "admin.py").read_text(encoding="utf-8")
        self.assertIn('action == "assign"', source)
        self.assertIn("ON DUPLICATE KEY UPDATE status='approved'", source)

    def test_disable_and_reenable_manage_session_marker(self):
        admin = (ROOT / "external_login_user" / "admin.py").read_text(encoding="utf-8")
        revocation = (ROOT / "external_login_user" / "session_revocation.py").read_text(encoding="utf-8")
        self.assertIn("revoke_external_user_sessions", admin)
        self.assertIn("mark_external_user_active", admin)
        self.assertIn("def mark_external_user_active", revocation)


if __name__ == "__main__":
    unittest.main()
