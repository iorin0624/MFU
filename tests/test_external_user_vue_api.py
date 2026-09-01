import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
API_PATH = ROOT / "external_login_user" / "user_api.py"
USERS_PATH = ROOT / "external_login_user" / "users.py"
UTILS_PATH = ROOT / "external_login_user" / "utils.py"
PAYMENTS_PATH = ROOT / "external_login_user" / "payments.py"
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
    def test_vue_is_the_standard_entry_with_an_immediate_legacy_rollback(self):
        api_source = API_PATH.read_text(encoding="utf-8-sig")
        users_source = USERS_PATH.read_text(encoding="utf-8-sig")
        self.assertIn('"/app"', api_source)
        self.assertIn('"/app/<path:vue_path>"', api_source)
        self.assertIn('"/legacy/"', users_source)
        self.assertIn('EXTERNAL_LOGIN_PORTAL_UI', users_source)
        index_source = function_source(USERS_PATH, "index")
        self.assertIn("user_vue_portal", index_source)
        self.assertIn("legacy_index", index_source)

    def test_login_choice_preserves_the_original_vue_destination(self):
        login = (VUE_FRONTEND_PATH / "views" / "LoginView.vue").read_text(encoding="utf-8-sig")
        app = (VUE_FRONTEND_PATH / "App.vue").read_text(encoding="utf-8-sig")
        store = (VUE_FRONTEND_PATH / "stores" / "portal.ts").read_text(encoding="utf-8-sig")
        guard = function_source(UTILS_PATH, "_require_ext_login")
        chooser = function_source(UTILS_PATH, "_external_login_choice_url")
        self.assertIn("route.query.next", login)
        self.assertIn("lineLoginUrl", login)
        self.assertIn("window.location.assign(returnUrl.value)", login)
        self.assertNotIn("/external-login/vue-preview/", login)
        self.assertIn("query: { next: route.fullPath }", app)
        self.assertIn("/external-login/app/login", store)
        self.assertIn("user_vue_portal", chooser)
        self.assertIn('"unauthorized"', guard)

    def test_profile_and_update_checkboxes_override_generic_form_layout(self):
        styles = (VUE_FRONTEND_PATH / "styles.css").read_text(encoding="utf-8-sig")
        self.assertIn(".profile-form label.toggle-line", styles)
        self.assertIn('.profile-form label.toggle-line input[type="checkbox"]', styles)
        self.assertIn(".modal-card label.update-seen", styles)
        self.assertIn('.modal-card label.update-seen input[type="checkbox"]', styles)

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
            '"/api/vue/events/<event_uuid>/my-process"',
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
            '"admin"',
        ):
            self.assertIn(marker, source)

    def test_vue_event_detail_exposes_participant_settings_and_restrictions(self):
        detail = (VUE_FRONTEND_PATH / "views" / "EventDetailView.vue").read_text(encoding="utf-8-sig")
        for marker in (
            "加工回し設定を保存",
            "参加区分・衣装／その他メモ",
            "実際の支払金額",
            "アンケート",
            "準備中",
            "参加申請は承認待ちです",
            "参加申請は承認されませんでした",
            "参加はキャンセル済みです",
            "イベント管理画面へ",
        ):
            self.assertIn(marker, detail)

    def test_vue_notifications_mark_all_excludes_unread_chat(self):
        notifications = (VUE_FRONTEND_PATH / "views" / "NotificationsView.vue").read_text(encoding="utf-8-sig")
        backend = (ROOT / "external_login_user" / "notifications.py").read_text(encoding="utf-8-sig")
        self.assertIn("markAllNotificationsRead", notifications)
        self.assertIn("window.confirm", notifications)
        self.assertIn("未読チャットは既読にはなりません", notifications)
        self.assertIn("COALESCE(kind,'') NOT IN ('chat_message', 'event_chat', 'dm')", backend)
        self.assertNotIn("deleteAllNotifications", notifications)
        self.assertNotIn('@bp.post("/api/notifications/delete-all")', backend)

    def test_vue_notification_counts_are_split_and_realtime(self):
        backend = (ROOT / "external_login_user" / "notifications.py").read_text(encoding="utf-8-sig")
        header = (VUE_FRONTEND_PATH / "components" / "AppHeader.vue").read_text(encoding="utf-8-sig")
        view = (VUE_FRONTEND_PATH / "views" / "NotificationsView.vue").read_text(encoding="utf-8-sig")
        realtime = (VUE_FRONTEND_PATH / "services" / "notificationRealtime.ts").read_text(encoding="utf-8-sig")
        self.assertIn('_CHAT_NOTIFICATION_KINDS = ("chat_message", "event_chat", "dm")', backend)
        self.assertIn('"notifications": notice_count', backend)
        self.assertIn('"chat": chat_count', backend)
        self.assertIn("totalUnread", header)
        self.assertIn("noticeUnread", header)
        self.assertIn("chatUnread", header)
        for marker in ("すべて", "未読", "お知らせ", "チャット"):
            self.assertIn(marker, view)
        self.assertIn("notif_unread", realtime)
        self.assertIn("30_000", realtime)

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
        self.assertIn("options.value.squareUrl", component)
        self.assertIn("event.urls.receipt", component)
        self.assertIn("Squareの安全な決済画面", component)
        self.assertIn("payment-paypay", (VUE_FRONTEND_PATH / "api" / "client.ts").read_text(encoding="utf-8-sig"))

    def test_square_payment_returns_to_the_vue_event_detail(self):
        payload = function_source(API_PATH, "_event_payload")
        start = function_source(PAYMENTS_PATH, "pay_start")
        complete = function_source(PAYMENTS_PATH, "pay_return")
        event_list = (VUE_FRONTEND_PATH / "views" / "EventListView.vue").read_text(encoding="utf-8-sig")
        self.assertIn('event_uuid=event_uuid, portal="vue"', payload)
        self.assertIn("name: 'event-payment'", event_list)
        self.assertNotIn(':href="item.urls.payment"', event_list)
        self.assertIn('"portal": "vue" if portal_vue else "legacy"', start)
        self.assertIn('portal="vue" if portal_vue else "legacy"', start)
        self.assertIn('pay_ctx.get("portal")', complete)
        self.assertIn('vue_path=f"events/{event_uuid}"', complete)

    def test_vue_payment_and_consent_endpoints_are_registered(self):
        source = API_PATH.read_text(encoding="utf-8-sig")
        for route in (
            '"/api/vue/privacy-policy/agree"',
            '"/api/vue/events/<event_uuid>/payment-options"',
            '"/api/vue/events/<event_uuid>/payment-paypay"',
            '"/api/vue/events/<event_uuid>/payment-bank"',
        ):
            self.assertIn(route, source)
        app = (VUE_FRONTEND_PATH / "App.vue").read_text(encoding="utf-8-sig")
        self.assertIn("agreePrivacyPolicy", app)
        self.assertNotIn('href="/external-login/">確認画面へ', app)
        guard = EXT_INIT_PATH.read_text(encoding="utf-8-sig")
        self.assertIn('"external_login_user.user_api_privacy_policy_agree"', guard)
        self.assertIn('"external_login_user.user_vue_preview"', guard)

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

    def test_participant_vue_actor_never_falls_back_to_mfu_login(self):
        source = function_source(API_PATH, "_actor")
        self.assertIn("_external_user", source)
        self.assertNotIn('session.get("user")', source)
        self.assertNotIn('"kind": "mfu"', source)

        payload = function_source(API_PATH, "_session_payload")
        self.assertNotIn('"mfuUsername"', payload)

        app = (VUE_FRONTEND_PATH / "App.vue").read_text(encoding="utf-8-sig")
        self.assertNotIn("またはMFUログイン", app)

    def test_vue_chat_admin_alias_is_limited_to_chat_and_notifications(self):
        payload = function_source(API_PATH, "_session_payload")
        self.assertIn('"chatAdminAlias"', payload)
        self.assertIn('"notificationScope"', payload)
        self.assertIn('_get_chat_admin_alias_ext_user_row', payload)

        notifications = (ROOT / "external_login_user" / "notifications.py").read_text(encoding="utf-8-sig")
        self.assertIn("COALESCE(kind,'') NOT IN ('chat_message', 'event_chat', 'dm')", notifications)

        client = (VUE_FRONTEND_PATH / "api" / "client.ts").read_text(encoding="utf-8-sig")
        self.assertIn("scope === 'mfu' ? '/api/mfu-notifications'", client)

        chat_actor = function_source(ROOT / "chat" / "__init__.py", "get_chat_actor")
        self.assertLess(chat_actor.index('ext_user_id = session.get("ext_user_id")'), chat_actor.index('if session.get("user")'))
        self.assertIn('"is_chat_admin_alias": True', chat_actor)

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
