import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AdminLogsDefaultLimitTest(unittest.TestCase):
    def test_default_limit_is_100_and_explicit_1000_remains_available(self):
        source = (ROOT / "__init__.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_build_admin_logs_html"
        )
        function_source = ast.get_source_segment(source, function) or ""
        self.assertIn('args_dict.get("limit", "100") or "100"', function_source)
        self.assertIn("per_page = 100", function_source)
        self.assertIn("min(1000, per_page)", function_source)

        template = (ROOT / "templates" / "admin_logs.html").read_text(encoding="utf-8-sig")
        self.assertIn("{% for n in [100,300,500,1000] %}", template)
        self.assertNotIn("filters.limit or 1000", template)


if __name__ == "__main__":
    unittest.main()
