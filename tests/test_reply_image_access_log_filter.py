import ast
import sqlite3
import unittest
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = "layer_reply.public_reply_image"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    node = next(item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef) and item.name == name)
    return ast.get_source_segment(source, node) or ""


class ReplyImageAccessLogFilterTest(unittest.TestCase):
    def test_only_successful_reply_image_requests_skip_storage(self):
        source = (ROOT / "utils" / "logs.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        constant = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "REPLY_IMAGE_ACCESS_LOG_ENDPOINT" for target in node.targets)
        )
        helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "is_successful_reply_image_access"
        )
        namespace = {"Optional": Optional}
        exec(compile(ast.Module(body=[constant, helper], type_ignores=[]), "logs.py", "exec"), namespace)
        skip = namespace["is_successful_reply_image_access"]

        for method, status in (("GET", 200), ("GET", 206), ("HEAD", 304)):
            self.assertTrue(skip(method, ENDPOINT, status))
        for method, endpoint, status in (
            ("GET", ENDPOINT, 403),
            ("GET", ENDPOINT, 404),
            ("POST", ENDPOINT, 200),
            ("GET", "layer_reply.view_reply", 200),
        ):
            self.assertFalse(skip(method, endpoint, status))

        log_access = _function_source(ROOT / "utils" / "logs.py", "log_access")
        self.assertIn("if is_successful_reply_image_access(", log_access)

    def test_admin_list_hides_only_successful_reply_images(self):
        source = _function_source(ROOT / "__init__.py", "_build_admin_logs_html")
        self.assertIn("params.append(REPLY_IMAGE_ACCESS_LOG_ENDPOINT)", source)
        tree = ast.parse(source)
        clause = next(
            ast.literal_eval(node.args[0])
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "where"
            and node.func.attr == "append"
            and node.args
            and "COALESCE(endpoint" in (ast.get_source_segment(source, node.args[0]) or "")
        )

        db = sqlite3.connect(":memory:")
        try:
            db.execute("CREATE TABLE logs (id INTEGER, endpoint TEXT, method TEXT, status INTEGER)")
            db.executemany(
                "INSERT INTO logs VALUES (?, ?, ?, ?)",
                [
                    (1, ENDPOINT, "GET", 200),
                    (2, ENDPOINT, "GET", 206),
                    (3, ENDPOINT, "HEAD", 304),
                    (4, ENDPOINT, "GET", 403),
                    (5, ENDPOINT, "GET", 404),
                    (6, ENDPOINT, "POST", 200),
                    (7, "layer_reply.view_reply", "GET", 200),
                    (8, None, "GET", 200),
                ],
            )
            ids = [row[0] for row in db.execute("SELECT id FROM logs WHERE " + clause.replace("%s", "?"), (ENDPOINT,))]
            self.assertEqual(ids, [4, 5, 6, 7, 8])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
