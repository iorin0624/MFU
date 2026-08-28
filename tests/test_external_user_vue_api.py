import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "external_login_user" / "user_api.py"
EXT_INIT_PATH = ROOT / "external_login_user" / "__init__.py"
APP_PATH = ROOT / "__init__.py"
ROUTES_PATH = ROOT / "albums" / "routes.py"
MIGRATION_PATH = ROOT / "migrations" / "20260828_album_child_creator.sql"


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


class ExternalUserVueApiSourceTests(unittest.TestCase):
    def test_mobile_vue_endpoints_are_registered(self):
        source = API_PATH.read_text(encoding="utf-8-sig")
        for route in (
            '"/api/vue/session"',
            '"/api/vue/bootstrap"',
            '"/api/vue/events"',
            '"/api/vue/events/<event_uuid>"',
            '"/api/vue/events/<event_uuid>/members"',
            '"/api/vue/events/<event_uuid>/my-role"',
            '"/api/vue/logout"',
        ):
            self.assertIn(route, source)

    def test_every_event_detail_request_rechecks_actor_access(self):
        source = function_source(API_PATH, "user_api_event")
        self.assertIn("_event_access(event, actor)", source)
        self.assertIn('return _error("forbidden", 403)', source)

    def test_event_access_supports_external_membership_admin_and_acl(self):
        source = function_source(API_PATH, "_event_access")
        self.assertIn("_latest_membership", source)
        self.assertIn('username == "admin"', source)
        self.assertIn("_event_acl_role", source)

    def test_event_permissions_require_active_approved_membership(self):
        source = function_source(API_PATH, "_event_permissions")
        self.assertIn('membership.get("status")', source)
        self.assertIn('membership.get("is_canceled")', source)
        self.assertIn('"canOpenAlbum"', source)
        self.assertIn('"canOpenChat"', source)

    def test_vue_mutations_are_csrf_protected_and_json(self):
        source = APP_PATH.read_text(encoding="utf-8-sig")
        self.assertIn('"/external-login/api/vue/",', source)
        json_source = function_source(APP_PATH, "_is_json_error_response")
        self.assertIn('request.path.startswith("/external-login/api/vue/")', json_source)

    def test_deleted_and_prerequisite_failures_return_json(self):
        source = EXT_INIT_PATH.read_text(encoding="utf-8-sig")
        self.assertIn('"account_deleted"', source)
        self.assertIn('"email_verification_required"', source)
        self.assertIn('"privacy_agreement_required"', source)

    def test_legacy_child_ownership_is_never_inferred_from_name(self):
        migration = MIGRATION_PATH.read_text(encoding="utf-8-sig")
        self.assertIn("created_by_ext_user_id", migration)
        self.assertNotIn("nickname", migration.lower())
        schema_source = function_source(ROUTES_PATH, "ensure_album_child_creator_schema")
        self.assertNotIn("nickname", schema_source.lower())


if __name__ == "__main__":
    unittest.main()
