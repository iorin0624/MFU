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
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


class EventAlbumAccessAuditTest(unittest.TestCase):
    def test_preview_detection_is_limited_to_event_album_endpoints(self):
        source = function_source(ROOT / "__init__.py", "_is_event_album_preview_request")
        self.assertIn("EVENT_ALBUM_PREVIEW_UA_TOKENS", source)
        self.assertIn('"album.album_home"', source)
        self.assertIn('"album.album_access"', source)
        self.assertIn("access_mode='event'", source)
        self.assertIn("event_id IS NOT NULL", source)

    def test_before_request_blocks_preview_and_marks_inapp_warning(self):
        source = function_source(ROOT / "__init__.py", "before_every_request")
        self.assertIn("_is_event_album_preview_request(request)", source)
        self.assertIn("[EVENT_ALBUM_PREVIEW_BLOCKED] アルバム未表示", source)
        self.assertIn("status=403", source)
        self.assertIn("[INAPP_WARNING] アルバム未表示", source)

    def test_album_home_records_granted_view_after_render(self):
        source = function_source(ROOT / "albums" / "routes.py", "album_home")
        render_at = source.index("rendered = render_template")
        audit_at = source.index("_write_event_album_view_granted")
        self.assertLess(render_at, audit_at)
        self.assertIn('album_meta.get("access_mode") == "event"', source)

    def test_granted_view_is_a_separate_audit_row(self):
        source = function_source(
            ROOT / "albums" / "routes.py",
            "_write_event_album_view_granted",
        )
        self.assertIn("log_request_raw(", source)
        self.assertIn("[ALBUM_VIEW_GRANTED]", source)
        self.assertIn('endpoint="album.album_view_granted"', source)
        self.assertIn('method="AUDIT"', source)

    def test_access_log_marker_is_stored_in_log_text(self):
        source = function_source(ROOT / "utils" / "logs.py", "log_request_raw")
        self.assertIn("if marker:", source)
        self.assertIn("parts.append(marker)", source)


if __name__ == "__main__":
    unittest.main()
