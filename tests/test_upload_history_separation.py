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


class UploadHistorySeparationTest(unittest.TestCase):
    def test_normal_delete_keeps_parent_row_and_layer_files(self):
        source = function_source(ROOT / "utils" / "upload_history.py", "upload_delete")
        self.assertIn("upload_deleted_at", source)
        self.assertNotIn("DELETE FROM uploads", source)
        self.assertNotIn("layer_uploads", source)

    def test_layer_delete_is_independent(self):
        source = function_source(ROOT / "utils" / "upload_history.py", "layer_upload_delete")
        self.assertIn("layer_deleted_at", source)
        self.assertIn("_layer_root()", source)
        self.assertNotIn("upload_deleted_at", source)

    def test_layer_reception_ignores_normal_deletion(self):
        source = function_source(ROOT / "utils" / "layer_reply.py", "layer_upload")
        self.assertIn("layer_deleted_at IS NULL", source)
        self.assertNotIn("upload_deleted_at", source)

    def test_normal_view_rejects_deleted_uploads(self):
        source = function_source(ROOT / "utils" / "upload_security.py", "fetch_upload_access_record")
        self.assertIn("upload_deleted_at IS NULL", source)


if __name__ == "__main__":
    unittest.main()
