import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "profile" / "formatting.py"
spec = importlib.util.spec_from_file_location("profile_formatting", MODULE_PATH)
profile_formatting = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(profile_formatting)


class ProfileFormattingTest(unittest.TestCase):
    def test_normalize_plain_text_for_display_converts_break_tags_and_crlf(self):
        value = "1行目<br>2行目\r\n3行目&lt;br /&gt;4行目&amp;lt;br&amp;gt;5行目"
        self.assertEqual(
            profile_formatting.normalize_plain_text_for_display(value),
            "1行目\n2行目\n3行目\n4行目\n5行目",
        )

    def test_linkify_plain_text_for_display_wraps_http_and_https_urls(self):
        value = "詳細は http://example.com と https://example.org/path?x=1 を参照"
        rendered = str(profile_formatting.linkify_plain_text_for_display(value))
        self.assertIn('<a href="http://example.com" target="_blank" rel="noopener noreferrer nofollow">http://example.com</a>', rendered)
        self.assertIn('<a href="https://example.org/path?x=1" target="_blank" rel="noopener noreferrer nofollow">https://example.org/path?x=1</a>', rendered)

    def test_linkify_plain_text_for_display_escapes_html_before_linkifying(self):
        value = '<script>alert(1)</script> https://example.com?q=<tag>'
        rendered = str(profile_formatting.linkify_plain_text_for_display(value))
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', rendered)
        self.assertIn('href="https://example.com?q=&lt;tag&gt;"', rendered)
        self.assertNotIn('<script>', rendered)


if __name__ == "__main__":
    unittest.main()
