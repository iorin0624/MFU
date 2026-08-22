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


class FakeCursor:
    def __init__(self, names):
        self.rows = [{"display_name": name} for name in names]

    def execute(self, _sql, _params):
        return None

    def fetchall(self):
        return list(self.rows)


def catalog_function(name: str):
    namespace = {
        "Path": Path,
        "_rows": lambda cursor: list(cursor.fetchall() or []),
    }
    exec(function_source(ROOT / "image_viewer" / "catalog.py", name), namespace)
    return namespace[name]


class ImageViewerNumberingTest(unittest.TestCase):
    def test_next_number_uses_highest_numeric_stem_across_extensions(self):
        next_display_name = catalog_function("_next_display_name")
        cursor = FakeCursor(["9.jpg", "11.png", "10.gif", "photo.webp"])

        self.assertEqual(next_display_name(cursor, 12, ".webp"), "12.webp")

    def test_original_name_gets_three_digit_collision_suffix(self):
        unique_display_name = catalog_function("_unique_display_name")
        cursor = FakeCursor(["photo.png", "photo_001.png", "PHOTO_002.PNG"])

        self.assertEqual(
            unique_display_name(cursor, 12, "photo.png"),
            "photo_003.png",
        )

    def test_original_name_is_preserved_when_available(self):
        unique_display_name = catalog_function("_unique_display_name")

        self.assertEqual(
            unique_display_name(FakeCursor(["other.png"]), 12, "撮影画像.png"),
            "撮影画像.png",
        )

    def test_upload_routes_forward_numbering_choice(self):
        source = function_source(
            ROOT / "image_viewer" / "routes.py", "_catalog_upload_response"
        )

        self.assertIn("_upload_numbering_enabled()", source)
        self.assertIn("ensure_unique_display_name=not numbering", source)


if __name__ == "__main__":
    unittest.main()
