"""Exercise production policy functions without importing the application."""
import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]


def functions(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    for node in selected:
        node.decorator_list = []
    exec(compile(ast.Module(body=selected, type_ignores=[]), path, "exec"), namespace)
    return namespace


class MutationPolicyTests(unittest.TestCase):
    def test_real_routes_reject_other_sender_expired_and_forged_admin_mode(self):
        class ReachedUpdate(Exception):
            pass
        for route in ("edit_message", "delete_message", "dm_edit_message", "dm_delete_message"):
            for own, admin, age, mode, allowed in [
                (True, False, 30, "cancel", True),
                (True, False, 61, "cancel", False),
                (False, False, 30, "cancel", False),
                (False, True, 30, "cancel", False),
                (True, False, 30, "admin", "delete" not in route),
                (False, True, 200, "admin", "delete" in route),
                (True, True, 200, "cancel", False),
            ]:
                with self.subTest(route=route, own=own, admin=admin, age=age, mode=mode):
                    actor = {"actor_type": "admin" if admin else "line", "actor_id": "1", "display_name": "A"}
                    key = actor["actor_type"] + ":1"
                    row = {"id": 1, "sender_actor_type": actor["actor_type"] if own else "line", "sender_actor_id": "1" if own else "2", "sender_actor_key": key if own else "line:2", "created_at": datetime.utcnow() - timedelta(minutes=age), "deleted_flag": 0}
                    def execute(sql, _params=()):
                        if sql.strip().startswith("UPDATE"):
                            raise ReachedUpdate()
                    cur = SimpleNamespace(execute=execute, fetchone=lambda: row, close=lambda: None)
                    db = SimpleNamespace(cursor=lambda **_: cur, close=lambda: None)
                    payload = {"csrf_token": "t", "dm_uuid": "d", "room_id": "r", "body": "A\nB", "body_text": "A\nB", "delete_mode": mode}
                    ns = {"Any": object, "datetime": datetime, "timedelta": timedelta, "get_chat_actor": lambda: actor, "get_chat_actor_key": lambda _: key, "request": SimpleNamespace(get_json=lambda **_: payload, form={}, args={}), "session": {"chat_csrf": "t"}, "jsonify": lambda value: value, "get_db": lambda: db, "can_access_dm": lambda *_: True, "_get_dm_conversation_by_uuid": lambda _: {"id": 1}, "_can_access_event": lambda *_: True, "_can_access_room": lambda *_: (True, "r", {}), "_is_admin_actor": lambda _: admin, "_is_chat_admin_actor": lambda _: admin, "_actor_sender_id": lambda t, i: t + ":" + i, "_check_rate_limit": lambda *a, **kw: True, "_validate_body": lambda value: value}
                    for schema in ("_ensure_chat_messages_room_schema", "_ensure_chat_thread_schema", "_ensure_chat_delete_schema", "_ensure_chat_edit_schema", "_ensure_chat_dm_schema", "_ensure_chat_dm_delete_schema", "_ensure_chat_dm_edit_schema"):
                        ns[schema] = lambda: True
                    functions("chat/__init__.py", {route, "_message_within_mutation_window", "_message_delete_notice"}, ns)
                    args = (1,) if route.startswith("dm_") else (1, 1)
                    if allowed:
                        with self.assertRaises(ReachedUpdate):
                            ns[route](*args)
                    else:
                        self.assertEqual(ns[route](*args)[1], 403)

    def test_one_hour_boundary_and_invalid_timestamp(self):
        ns = functions("chat/__init__.py", {"_message_within_mutation_window"}, {"datetime": datetime, "timedelta": timedelta, "Any": object})
        check = ns["_message_within_mutation_window"]
        now = datetime(2026, 9, 1)
        self.assertTrue(check(now - timedelta(hours=1), now))
        self.assertFalse(check(now - timedelta(hours=1, microseconds=1), now))
        self.assertFalse(check(now + timedelta(seconds=1), now))
        self.assertFalse(check(None, now))

    def test_cancellation_and_admin_notice(self):
        ns = functions("chat/__init__.py", {"_message_delete_notice"}, {})
        self.assertEqual(ns["_message_delete_notice"]({"display_name": "A"}, False), "Aさんが取り消しました。")
        self.assertEqual(ns["_message_delete_notice"]({"display_name": "admin"}, True), "管理者adminにより削除")

    def test_participant_template_type_is_enforced(self):
        ns = functions("albums/api.py", {"_validate_child_template"}, {"_can_choose_child_type": lambda ctx: ctx.get("free", False)})
        check = ns["_validate_child_template"]
        for prefix, mode in [("【構図】", "normal"), ("【オフショ】", "normal"), ("【動画】", "movie"), ("【加工回し】", "process")]:
            self.assertTrue(check({}, prefix + "Aさん", mode))
            for other in {"normal", "movie", "process"} - {mode}:
                self.assertFalse(check({}, prefix + "Aさん", other))
            self.assertFalse(check({}, prefix, mode))
        self.assertFalse(check({}, "Aさん", "normal"))
        self.assertTrue(check({"free": True}, "Aさん", "movie"))

    def test_host_privilege_does_not_come_from_chat_alias(self):
        ns = functions("albums/api.py", {"_can_choose_child_type"}, {"db_get_one": lambda *_: {"is_host": 1}})
        check = ns["_can_choose_child_type"]
        self.assertFalse(check({"is_chat_admin_alias": True}))
        self.assertFalse(check({"event_acl_role": "viewer"}))
        self.assertTrue(check({"event_acl_role": "manager"}))
        self.assertTrue(check({"is_admin": True}))
        self.assertTrue(check({"event_member": True, "current_ext_user_id": 1, "gate": {"event_id": 1}}))

    def test_all_four_routes_enforce_policy(self):
        tree = ast.parse((ROOT / "chat/__init__.py").read_text(encoding="utf-8-sig"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in {"edit_message", "delete_message", "dm_edit_message", "dm_delete_message"}:
                source = ast.unparse(node)
                self.assertIn("_message_within_mutation_window", source)
                if "delete" in node.name:
                    self.assertIn("delete_mode", source)
                    self.assertIn("delete_notice", source)
                else:
                    self.assertNotIn("can_edit = True", source)


if __name__ == "__main__":
    unittest.main()
