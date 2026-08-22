from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHAT_SOURCE = (ROOT / "chat" / "__init__.py").read_text(encoding="utf-8")
CHAT_TREE = ast.parse(CHAT_SOURCE)


def _function_source(name: str) -> str:
    node = next(
        item
        for item in CHAT_TREE.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    return ast.get_source_segment(CHAT_SOURCE, node) or ""


class ExternalLoginSessionRevocationTest(unittest.TestCase):
    def test_socket_connect_checks_database_and_tracks_external_user_socket(self):
        source = _function_source("chat_connect")

        self.assertIn("is_external_user_active(ext_user_id, force_refresh=True)", source)
        self.assertIn('join_room(f"external_user:{ext_user_id}")', source)
        self.assertIn("register_external_user_socket(ext_user_id, _socket_sid())", source)
        self.assertIn("_remember_socket_actor(actor)", source)

    def test_socket_disconnect_removes_tracked_sid(self):
        source = _function_source("chat_disconnect")

        self.assertIn("_forget_socket_actor()", source)
        self.assertIn("unregister_external_user_socket(ext_user_id, sid)", source)

    def test_important_socket_actions_recheck_revocation_status(self):
        for function_name in ("on_send", "on_react", "on_dm_react", "notify_dm"):
            source = _function_source(function_name)
            self.assertIn("_get_socket_actor()", source)
            self.assertIn("_disconnect_if_external_user_revoked(actor)", source)

    def test_seen_and_typing_do_not_query_revocation_status(self):
        for function_name in ("on_seen", "on_typing"):
            source = _function_source(function_name)
            self.assertIn("_get_cached_socket_actor()", source)
            self.assertNotIn("_disconnect_if_external_user_revoked", source)
            self.assertNotIn("is_external_user_active", source)
            self.assertNotIn("get_chat_actor()", source)

    def test_deletion_revokes_push_subscriptions_and_live_sessions(self):
        source = (ROOT / "external_login_user" / "deletion_service.py").read_text(encoding="utf-8")

        self.assertIn("DELETE FROM chat_push_subscriptions", source)
        self.assertIn("revoke_external_user_sessions(int(user_id))", source)
        self.assertIn("future_memberships_canceled", source)

    def test_revocation_uses_shared_redis_and_socketio_queue(self):
        source = (ROOT / "external_login_user" / "session_revocation.py").read_text(encoding="utf-8")

        self.assertIn('current_app.config.get("SOCKETIO_MESSAGE_QUEUE")', source)
        self.assertIn('os.getenv("SOCKETIO_MESSAGE_QUEUE")', source)
        self.assertIn("socketio.emit(", source)
        self.assertIn('"force_logout"', source)
        self.assertIn('to=f"external_user:{uid}"', source)
        self.assertIn("ignore_queue=False", source)
        self.assertIn("EXTERNAL_USER_STATUS_CACHE_TTL_SECONDS", source)

    def test_all_external_chat_clients_handle_force_logout(self):
        files = (
            ROOT / "chat" / "templates" / "chat" / "room.html",
            ROOT / "chat" / "templates" / "chat" / "_chat_nav.html",
            ROOT / "chat" / "static" / "chat" / "js" / "chat.js",
            ROOT / "external_login_user" / "template" / "_extlogin_nav.html",
        )

        for path in files:
            source = path.read_text(encoding="utf-8")
            self.assertIn("socket.on('force_logout'", source)
            self.assertIn("socket.disconnect()", source)
            self.assertIn("window.location.replace(redirectUrl)", source)


if __name__ == "__main__":
    unittest.main()
