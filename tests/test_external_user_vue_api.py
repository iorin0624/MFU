import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "external_login_user" / "user_api.py"
UTILS_PATH = ROOT / "external_login_user" / "utils.py"
VUE_TEMPLATE_PATH = ROOT / "external_login_user" / "template" / "external_login_vue.html"
VUE_FRONTEND_PATH = ROOT / "external_login_user" / "frontend" / "src"
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
    def test_preview_shell_is_registered_without_replacing_legacy_routes(self):
        source = API_PATH.read_text(encoding="utf-8-sig")
        self.assertIn('"/vue-preview"', source)
        self.assertIn('"/vue-preview/"', source)
        self.assertIn('"/vue-preview/<path:vue_path>"', source)
        self.assertIn('"external_login_vue.html"', source)
        template = VUE_TEMPLATE_PATH.read_text(encoding="utf-8-sig")
        self.assertIn("event-portal.js", template)
        self.assertIn("event-portal.css", template)

    def test_vue_profile_loads_the_saved_privacy_agreement_revision(self):
        source = function_source(UTILS_PATH, "_get_ext_user_by_social")
        self.assertIn("privacy_policy_agreed_revised_date", source)

    def test_participant_vue_contains_requested_event_and_album_views(self):
        for relative in (
            "views/EventListView.vue",
            "views/EventDetailView.vue",
            "views/EventPassView.vue",
            "views/EventMembersView.vue",
            "views/EventSocialView.vue",
            "views/NotificationsView.vue",
            "views/ProfileView.vue",
            "views/LoginView.vue",
            "views/EmailVerifyView.vue",
            "views/JoinEventView.vue",
            "views/EventPaymentView.vue",
            "views/AlbumView.vue",
            "views/ChildAlbumView.vue",
            "components/AppHeader.vue",
            "components/PortalUtilities.vue",
            "components/PortalFooter.vue",
        ):
            self.assertTrue((VUE_FRONTEND_PATH / relative).is_file(), relative)

    def test_mobile_vue_endpoints_are_registered(self):
        source = API_PATH.read_text(encoding="utf-8-sig")
        for route in (
            '"/api/vue/session"',
            '"/api/vue/bootstrap"',
            '"/api/vue/events"',
            '"/api/vue/events/<event_uuid>"',
            '"/api/vue/events/<event_uuid>/pass"',
            '"/api/vue/events/<event_uuid>/members"',
            '"/api/vue/events/<event_uuid>/participants-email"',
            '"/api/vue/events/<event_uuid>/my-role"',
            '"/api/vue/events/<event_uuid>/join"',
            '"/api/vue/profile"',
            '"/api/vue/email-verification/send"',
            '"/api/vue/email-verification/verify"',
            '"/api/vue/logout"',
        ):
            self.assertIn(route, source)

    def test_vue_event_payload_exposes_migrated_participant_features(self):
        source = function_source(API_PATH, "_event_payload")
        for marker in (
            '"lineOpenchatPass"',
            '"participantMemo"',
            '"payFrom"',
            '"payUntil"',
            '"tipEnabled"',
            '"receipt"',
            '"participantsEmail"',
        ):
            self.assertIn(marker, source)

    def test_event_chat_and_line_openchat_are_exclusive(self):
        source = function_source(API_PATH, "_event_payload")
        self.assertIn('str(event.get("line_openchat_url") or "").strip()', source)
        detail = (VUE_FRONTEND_PATH / "views" / "EventDetailView.vue").read_text(encoding="utf-8-sig")
        self.assertIn("!event.lineOpenchatUrl", detail)

    def test_member_links_and_sns_copy_are_separate_views(self):
        members = (VUE_FRONTEND_PATH / "views" / "EventMembersView.vue").read_text(encoding="utf-8-sig")
        social = (VUE_FRONTEND_PATH / "views" / "EventSocialView.vue").read_text(encoding="utf-8-sig")
        self.assertIn("https://x.com", members)
        self.assertIn("www.instagram.com", members)
        self.assertIn("SNS貼付用", social)
        self.assertNotIn("X / Instagramリンク", social)

    def test_vue_account_and_notification_navigation_stays_inside_portal(self):
        source = function_source(API_PATH, "_navigation")
        self.assertIn('/notifications', source)
        self.assertIn('/profile', source)
        header = (VUE_FRONTEND_PATH / "components" / "AppHeader.vue").read_text(encoding="utf-8-sig")
        self.assertIn("router.push('/notifications')", header)
        self.assertIn("router.push('/profile')", header)

    def test_payment_view_hands_sensitive_input_to_existing_square_route(self):
        component = (VUE_FRONTEND_PATH / "views" / "EventPaymentView.vue").read_text(encoding="utf-8-sig")
        self.assertIn("event.urls.payment", component)
        self.assertIn("event.urls.receipt", component)
        self.assertIn("Square決済画面", component)

    def test_vue_session_exposes_document_links(self):
        source = function_source(API_PATH, "_session_payload")
        self.assertIn('"privacyPolicyUrl"', source)
        self.assertIn('"commerceLawUrl"', source)
        self.assertIn('"participantTermsUrl"', source)

    def test_participant_png_email_reuses_legacy_authorization_and_job(self):
        source = function_source(API_PATH, "user_api_participants_email")
        self.assertIn("_can_send_participants_png_mail", source)
        self.assertIn("_start_participants_png_email_job", source)
        self.assertIn("email_verified_at", source)

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
        self.assertIn('"canOpenPass"', source)

    def test_vue_participant_pass_rechecks_external_approved_membership(self):
        source = function_source(API_PATH, "user_api_event_pass")
        self.assertIn('actor.get("kind") != "external"', source)
        self.assertIn('_latest_membership', source)
        self.assertIn('membership.get("status")', source)
        self.assertIn('membership.get("is_canceled")', source)
        self.assertIn('"venue_qr" if checked_in else None', source)

    def test_vue_participant_pass_does_not_generate_a_pass_qr_code(self):
        source = function_source(API_PATH, "user_api_event_pass")
        component = (VUE_FRONTEND_PATH / "views" / "EventPassView.vue").read_text(encoding="utf-8-sig")
        self.assertNotIn("qrcode", source.lower())
        self.assertNotIn("qr-code", component.lower())
        self.assertIn("会場に掲示されたQRコード", component)

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
