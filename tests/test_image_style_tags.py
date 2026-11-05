import importlib.util
import tempfile
import unittest
from pathlib import Path

_module_path = Path(__file__).resolve().parents[1] / "image_viewer" / "style_tags.py"
_spec = importlib.util.spec_from_file_location("image_viewer_style_tags_test", _module_path)
assert _spec and _spec.loader
style_tags = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(style_tags)
MAJOR_STYLES = style_tags.MAJOR_STYLES
delete_assignment = style_tags.delete_assignment
get_assignment = style_tags.get_assignment
normalize_assignment = style_tags.normalize_assignment
put_assignment = style_tags.put_assignment


class ImageStyleTagsTest(unittest.TestCase):
    def test_major_styles_are_fixed_and_middle_small_are_multiple(self):
        self.assertEqual([item["id"] for item in MAJOR_STYLES], ["lolita", "jirai", "casual"])
        value = normalize_assignment({
            "major": "lolita",
            "middleTags": [
                {"name": "クラシカル", "smallTags": ["青", "長袖"]},
                {"name": "ゴシック", "smallTags": ["黒", "レース"]},
            ],
        })
        self.assertEqual(value["middleTags"][0]["smallTags"], ["青", "長袖"])
        self.assertEqual(value["middleTags"][1]["name"], "ゴシック")

    def test_rejects_other_major_and_duplicates(self):
        for value in (
            {"major": "disney", "middleTags": []},
            {"major": "casual", "middleTags": [{"name": "外出", "smallTags": ["青", "青"]}]},
            {"major": "jirai", "middleTags": [{"name": "黒", "smallTags": []}, {"name": "黒", "smallTags": []}]},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_assignment(value)

    def test_stable_checksum_key_and_clear(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "styles.sqlite3"
            self.assertIsNone(get_assignment("a" * 64, db_path))
            saved = put_assignment("a" * 64, {
                "major": "casual", "middleTags": [{"name": "ディズニー", "smallTags": ["アナ風"]}],
            }, db_path)
            self.assertEqual(get_assignment("a" * 64, db_path), saved)
            delete_assignment("a" * 64, db_path)
            self.assertIsNone(get_assignment("a" * 64, db_path))


if __name__ == "__main__":
    unittest.main()
