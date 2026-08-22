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


class ImageViewerMultiSelectTest(unittest.TestCase):
    def test_batch_response_keeps_successes_and_failures_separate(self):
        namespace = {}
        exec(
            function_source(
                ROOT / "image_viewer" / "routes.py", "_batch_entry_response"
            ),
            namespace,
        )
        batch_response = namespace["_batch_entry_response"]

        def operation(entry):
            if entry["path"] == "bad.jpg":
                return {"ok": False, "error": "conflict"}, 409
            return {"ok": True, "path": f"dest/{entry['path']}"}, 200

        result = batch_response(
            {
                "destination": "dest",
                "entries": [
                    {"type": "file", "path": "good.jpg"},
                    {"type": "file", "path": "bad.jpg"},
                ],
            },
            operation,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["results"][1]["status"], 409)
        self.assertEqual(result["results"][1]["sourcePath"], "bad.jpg")

    def test_batch_response_rejects_more_than_500_entries(self):
        namespace = {}
        exec(
            function_source(
                ROOT / "image_viewer" / "routes.py", "_batch_entry_response"
            ),
            namespace,
        )
        result = namespace["_batch_entry_response"](
            {"entries": [{"path": str(index)} for index in range(501)]},
            lambda entry: ({"ok": True}, 200),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["results"], [])

    def test_template_contains_requested_selection_and_batch_actions(self):
        source = (ROOT / "image_viewer" / "template" / "image_viewer.html").read_text(
            encoding="utf-8-sig"
        )

        for expected in (
            "selectFileFromEvent",
            "event.shiftKey",
            "wireMarqueeSelection",
            "deleteEntries",
            "moveEntries",
            "pasteInternalClipboard",
        ):
            self.assertIn(expected, source)


if __name__ == "__main__":
    unittest.main()
