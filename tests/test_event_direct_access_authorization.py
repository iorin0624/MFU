import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    node = next(
        item
        for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


class EventDirectAccessAuthorizationTest(unittest.TestCase):
    def test_event_pass_requires_approved_non_canceled_membership(self):
        source = function_source(
            ROOT / "external_login_user" / "users.py",
            "event_pass",
        )
        self.assertIn('str(my_status).strip().lower() != "approved"', source)
        self.assertIn('int(row.get("is_canceled") or 0) == 1', source)
        self.assertIn('abort(403, "承認済みの参加者のみ参加証を利用できます")', source)

    def test_event_chat_requires_approved_non_canceled_membership(self):
        source = function_source(
            ROOT / "chat" / "__init__.py",
            "_has_active_event_membership",
        )
        self.assertIn(
            'str(row.get("status") or "").strip().lower() == "approved"',
            source,
        )
        self.assertIn('int(row.get("is_canceled") or 0) == 0', source)


if __name__ == "__main__":
    unittest.main()
