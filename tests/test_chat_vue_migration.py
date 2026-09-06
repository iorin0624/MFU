from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ChatVueMigrationSourceTests(unittest.TestCase):
    def test_participant_vue_has_chat_routes(self):
        router = (ROOT / "external_login_user/frontend/src/router.ts").read_text(encoding="utf-8-sig")
        self.assertIn("/chat", router)
        self.assertIn("/events/:uuid/chat", router)
        self.assertIn("ChatDmView", router)

    def test_chat_uses_shared_realtime_connection(self):
        chat = (ROOT / "external_login_user/frontend/src/stores/chat.ts").read_text(encoding="utf-8-sig")
        notification = (ROOT / "external_login_user/frontend/src/services/notificationRealtime.ts").read_text(encoding="utf-8-sig")
        self.assertIn("portalRealtime", chat)
        self.assertIn("portalRealtime", notification)
        self.assertNotIn("window.io(", notification)

    def test_vue_chat_api_requires_existing_session(self):
        api = (ROOT / "chat/gui_api.py").read_text(encoding="utf-8-sig")
        self.assertIn('@chat_bp.get("/api/vue/bootstrap")', api)
        self.assertIn('@chat_bp.post("/api/vue/dm/open")', api)
        self.assertIn("actor = get_chat_actor()", api)
        self.assertNotIn('/api/gui/login', api)
        self.assertNotIn('/api/gui/', api)

    def test_mfu_chat_scope_uses_the_vue_screen_without_becoming_a_participant_session(self):
        chat = (ROOT / "chat/__init__.py").read_text(encoding="utf-8-sig")
        client = (ROOT / "external_login_user/frontend/src/api/client.ts").read_text(encoding="utf-8-sig")
        app = (ROOT / "external_login_user/frontend/src/App.vue").read_text(encoding="utf-8-sig")
        event_chat = (ROOT / "external_login_user/frontend/src/views/EventChatView.vue").read_text(encoding="utf-8-sig")
        realtime = (ROOT / "external_login_user/frontend/src/services/portalRealtime.ts").read_text(encoding="utf-8-sig")
        self.assertIn('query["auth_scope"] = "mfu"', chat)
        self.assertIn('X-Chat-Auth-Scope', client)
        self.assertIn('!store.session.authenticated && !mfuChatAllowed', app)
        self.assertIn("route.query.auth_scope === 'mfu'", event_chat)
        self.assertIn('chat_auth_scope', realtime)

    def test_chat_bootstrap_events_include_uuid_for_mfu_navigation(self):
        backend = (ROOT / "chat/__init__.py").read_text(encoding="utf-8-sig")
        view = (ROOT / "external_login_user/frontend/src/views/ChatListView.vue").read_text(encoding="utf-8-sig")
        self.assertIn("SELECT id, event_uuid, title", backend)
        self.assertIn("uuid: item.event_uuid", view)

    def test_legacy_chat_index_and_mfu_nav_notifications_use_mfu_vue_scope(self):
        backend = (ROOT / "chat/__init__.py").read_text(encoding="utf-8-sig")
        nav = (ROOT / "templates/base.html").read_text(encoding="utf-8-sig")
        self.assertIn('return redirect("/external-login/app/chat?auth_scope=mfu")', backend)
        self.assertIn("target.searchParams.set('auth_scope', 'mfu')", nav)
        self.assertIn("targetUrl = '/external-login/app/chat?auth_scope=mfu'", nav)

    def test_core_legacy_features_are_wired(self):
        pane = (ROOT / "external_login_user/frontend/src/components/ChatRoomPane.vue").read_text(encoding="utf-8-sig")
        manager = (ROOT / "external_login_user/frontend/src/components/ChatRoomManager.vue").read_text(encoding="utf-8-sig")
        for feature in ("chatDmUploadImages", "chatDmSearch", "chatDmEdit", "chatDmDelete", "sendThread", "chatReactionDetails"):
            self.assertIn(feature, pane)
        for feature in ("pendingPreviews", "dropImages", "画像を送信", "chat-reaction-trigger", "jumpLatest", "openLightbox", "copyMessage", "showDateDivider"):
            self.assertIn(feature, pane)
        for feature in ("messagePointerDown", "messageDoubleClick", "openReadDetails", "chat-message-menu", "response.groups"):
            self.assertIn(feature, pane)
        for feature in ("chatCreateRoom", "chatUpdateRoom", "chatSetRoomMembers", "chatDeleteRoom"):
            self.assertIn(feature, manager)

    def test_room_and_dm_unread_counts_are_not_placeholder_values(self):
        api = (ROOT / "chat/gui_api.py").read_text(encoding="utf-8-sig")
        client = (ROOT / "external_login_user/frontend/src/api/client.ts").read_text(encoding="utf-8-sig")
        store = (ROOT / "external_login_user/frontend/src/stores/chat.ts").read_text(encoding="utf-8-sig")
        self.assertIn("SELECT COUNT(*) AS unread_count", api)
        self.assertIn("chatRoomUnread", client)
        self.assertIn("refreshRoomUnread", store)
        self.assertIn("markChatRoomNotificationsRead", client)
        self.assertIn("markCurrentNotificationsRead", store)


if __name__ == "__main__":
    unittest.main()
