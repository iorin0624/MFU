import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FakeCursor:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))


class FakeDb:
    def __init__(self):
        self.cur = FakeCursor()
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def load_function(path: Path, name: str, namespace: dict):
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


class ExternalLoginBlockAuditTest(unittest.TestCase):
    def test_blocked_line_login_is_written_with_original_user_id(self):
        db = FakeDb()
        write_blocked = load_function(
            ROOT / "utils" / "logs.py",
            "write_line_login_blocked_log",
            {"get_db": lambda: db},
        )

        write_blocked("203.0.113.10", original_user_id=47)

        self.assertTrue(db.committed)
        self.assertTrue(db.closed)
        self.assertEqual(len(db.cur.calls), 1)
        sql, params = db.cur.calls[0]
        self.assertIn("INSERT INTO logs", sql)
        self.assertEqual(params[0], "203.0.113.10")
        self.assertEqual(
            params[1],
            "[LINE_LOGIN_BLOCKED] 退会済みLINEアカウントからのログインを拒否しました original_user_id=47",
        )
        self.assertNotIn("social_id", params[1])

    def test_line_callback_calls_block_audit_helper(self):
        source = (ROOT / "external_login_user" / "users.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        callback = next(
            item
            for item in ast.walk(tree)
            if isinstance(item, ast.FunctionDef) and item.name == "line_callback"
        )
        callback_source = ast.get_source_segment(source, callback) or ""

        self.assertIn("if identity_locked:", callback_source)
        self.assertIn("original_user_id=original_user_id", callback_source)
        self.assertIn('request.headers.get("CF-Connecting-IP")', callback_source)
        self.assertIn('request.headers.get("X-Forwarded-For"', callback_source)


if __name__ == "__main__":
    unittest.main()
