import importlib.util
import unittest
from datetime import datetime
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "external_login_user" / "album_naming.py"
SPEC = importlib.util.spec_from_file_location("event_album_naming", MODULE_PATH)
album_naming = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(album_naming)


class EventAlbumNamingTest(unittest.TestCase):
    def test_formats_datetime_with_full_width_separators(self):
        self.assertEqual(
            album_naming.format_event_album_name(
                title="アナだらけのパーティー",
                starts_at=datetime(2025, 12, 6, 13, 30),
            ),
            "【イベント】　2025年12月06日　アナだらけのパーティー",
        )

    def test_formats_iso_datetime_string(self):
        self.assertEqual(
            album_naming.format_event_album_name(
                title=" テストイベント ",
                starts_at="2026-07-18T10:00",
            ),
            "【イベント】　2026年07月18日　テストイベント",
        )

    def test_omits_date_until_event_date_is_set(self):
        self.assertEqual(
            album_naming.format_event_album_name(title="テストイベント", starts_at=None),
            "【イベント】　テストイベント",
        )


if __name__ == "__main__":
    unittest.main()
