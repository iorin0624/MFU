import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    node = next(
        item for item in ast.walk(tree)
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


class AdminLogsPathSearchTest(unittest.TestCase):
    def test_concrete_url_path_uses_prefix_search_without_log_text_or(self):
        source = function_source(ROOT / "__init__.py", "_build_admin_logs_html")
        self.assertIn('search_path.startswith("/")', source)
        self.assertIn('where.append("path LIKE %s")', source)
        self.assertIn('_adminlogs_like(search_path, mode="prefix")', source)
        self.assertIn("not has_path_prefix_filter", source)

    def test_path_index_is_preferred_for_concrete_path(self):
        source = function_source(ROOT / "__init__.py", "_build_admin_logs_html")
        self.assertIn("has_path_prefix_filter and _adminlogs_has_path_index(cursor)", source)
        self.assertIn('logs FORCE INDEX (idx_logs_path)', source)

    def test_migration_adds_prefix_index_for_path(self):
        migration = (ROOT / "migrations" / "20260713_logs_path_index.sql").read_text(encoding="utf-8-sig")
        self.assertIn("CREATE INDEX idx_logs_path ON logs (path(191))", migration)


if __name__ == "__main__":
    unittest.main()
