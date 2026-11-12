import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
except ImportError:
    Environment = FileSystemLoader = select_autoescape = None

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

    def test_default_qa_has_two_questions(self):
        items = profile_formatting.parse_profile_qa_text(profile_formatting.DEFAULT_PROFILE_QA_TEXT)
        self.assertEqual([item["number"] for item in items], [1, 2])
        self.assertIn("撮影の依頼", items[0]["question"])
        self.assertIn("新規のご依頼", items[1]["answer"])

    def test_qa_parser_preserves_multiline_answers_and_blank_lines(self):
        items = profile_formatting.parse_profile_qa_text(
            "Q1. 最初の質問\r\n一行目\r\n\r\n二行目\r\n\r\nQ2. 次の質問\r\n回答"
        )
        self.assertEqual(items[0]["answer"], "一行目\n\n二行目")
        self.assertEqual(items[1]["question"], "次の質問")

    def test_qa_parser_reads_optional_category_and_summary_without_changing_answer(self):
        items = profile_formatting.parse_profile_qa_text(
            "Q1. 納品方法は？\n【カテゴリ】納品・保存\n【要点】専用サービスでお渡しします。\n回答の一行目\n\n回答の二行目\n"
            "Q2. 次の質問\n回答"
        )
        self.assertEqual(items[0]["category"], "納品・保存")
        self.assertEqual(items[0]["summary"], "専用サービスでお渡しします。")
        self.assertEqual(items[0]["answer"], "回答の一行目\n\n回答の二行目")
        self.assertEqual(items[1]["summary"], "")
        self.assertEqual(items[1]["category"], "その他")
        self.assertEqual(
            profile_formatting.list_profile_qa_categories(items),
            [{"name": "納品・保存", "count": 1}, {"name": "その他", "count": 1}],
        )

    def test_untagged_existing_questions_get_suggested_categories(self):
        items = profile_formatting.parse_profile_qa_text(
            "Q1. 撮影の依頼方法は？\n回答\n"
            "Q2. 支払い方法は？\n回答\n"
            "Q3. キャンセルできますか？\n回答\n"
            "Q4. 写真はいつ受け取れますか？\n回答\n"
            "Q5. SNSに掲載できますか？\n回答"
        )
        self.assertEqual(
            [item["category"] for item in items],
            ["依頼・日程", "費用・支払い", "キャンセル・変更", "納品・保存", "写真の利用"],
        )

    def test_qa_metadata_is_validated(self):
        for text, message in (
            ("Q1. 質問\n【要点】\n回答", "要点は1"),
            ("Q1. 質問\n【カテゴリ】その他\n【カテゴリ】納品・保存\n回答", "カテゴリは1問"),
            ("Q1. 質問\n回答\n【要点】後から追加", "回答本文より前"),
            ("Q1. 質問\n【要点】" + "あ" * 201 + "\n回答", "要点は1"),
        ):
            with self.subTest(text=text[:40]), self.assertRaisesRegex(ValueError, message):
                profile_formatting.parse_profile_qa_text(text)

    def test_qa_parser_rejects_missing_answer_and_number_gap(self):
        with self.assertRaisesRegex(ValueError, "Q1の回答"):
            profile_formatting.parse_profile_qa_text("Q1. 質問\n\nQ2. 次の質問\n回答")
        with self.assertRaisesRegex(ValueError, "Q2. から順番"):
            profile_formatting.parse_profile_qa_text("Q1. 質問\n回答\nQ3. 次の質問\n回答")

    def test_qa_parser_rejects_text_before_first_question_and_malformed_heading(self):
        with self.assertRaisesRegex(ValueError, "最初の質問"):
            profile_formatting.parse_profile_qa_text("前置き\nQ1. 質問\n回答")
        with self.assertRaisesRegex(ValueError, "質問は"):
            profile_formatting.parse_profile_qa_text("Q1 質問\n回答")

    def test_qa_parser_empty_text_hides_section(self):
        self.assertEqual(profile_formatting.parse_profile_qa_text("  \n "), [])

    def test_qa_update_date_uses_japan_calendar_day(self):
        self.assertIsNone(profile_formatting.format_qa_updated_at(None))
        self.assertEqual(
            profile_formatting.format_qa_updated_at(datetime(2026, 11, 11, 15, 30)),
            "2026/11/12更新",
        )
        self.assertEqual(
            profile_formatting.format_qa_updated_at(datetime(2026, 11, 11, 14, 59, tzinfo=timezone.utc)),
            "2026/11/11更新",
        )

    def test_legacy_qa_is_extracted_without_losing_request_notes(self):
        original = "撮影依頼の案内\n別の段落\n\nQ&A\n\nQ1. 最初の質問\n回答\n\nQ2. 次の質問\n二つ目の回答"
        notes, qa = profile_formatting.split_legacy_profile_qa(original)
        self.assertEqual(notes, "撮影依頼の案内\n別の段落")
        self.assertEqual(len(profile_formatting.parse_profile_qa_text(qa)), 2)
        self.assertIsNone(profile_formatting.split_legacy_profile_qa("撮影依頼の案内のみ"))

    @unittest.skipIf(Environment is None, "Jinja2 is not installed")
    def test_public_qa_uses_independent_closed_details_and_escapes_text(self):
        items = profile_formatting.parse_profile_qa_text(
            "Q1. <script>質問</script>\n回答\n\n続き\nQ2. 次の質問\n回答"
        )
        for item in items:
            item["answer_html"] = profile_formatting.linkify_plain_text_for_display(str(item["answer"]))
        templates = MODULE_PATH.parent / "template"
        env = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape(["html"]))
        rendered = env.get_template("profile_public.html").render(
            profile={"page_title": "プロフィール", "display_name": "テスト"},
            grouped_works=[], qa_items=items, qa_updated_label="2026/11/11更新",
        )
        self.assertEqual(rendered.count('<details class="profile-qa-item'), 2)
        self.assertIn("すべて開く", rendered)
        self.assertIn("すべて閉じる", rendered)
        self.assertNotIn("data-bs-parent", rendered)
        self.assertIn("&lt;script&gt;質問&lt;/script&gt;", rendered)
        self.assertNotIn("<script>質問</script>", rendered)
        self.assertLess(rendered.rindex("Q2."), rendered.index("2026/11/11更新"))

    @unittest.skipIf(Environment is None, "Jinja2 is not installed")
    def test_public_qa_renders_search_categories_and_escaped_summary(self):
        items = profile_formatting.parse_profile_qa_text(
            "Q1. 納品方法は？\n【カテゴリ】納品・保存\n【要点】<script>要点</script>\n回答"
        )
        items[0]["answer_html"] = profile_formatting.linkify_plain_text_for_display(str(items[0]["answer"]))
        env = Environment(
            loader=FileSystemLoader(MODULE_PATH.parent / "template"),
            autoescape=select_autoescape(["html"]),
        )
        rendered = env.get_template("profile_public.html").render(
            profile={"page_title": "プロフィール", "display_name": "テスト"},
            grouped_works=[], qa_items=items,
            qa_categories=profile_formatting.list_profile_qa_categories(items),
        )
        self.assertIn('id="profile-qa-search"', rendered)
        self.assertIn('data-qa-category-filter="納品・保存"', rendered)
        self.assertIn('data-qa-category="納品・保存"', rendered)
        self.assertIn('要点：&lt;script&gt;要点&lt;/script&gt;', rendered)
        self.assertNotIn('<script>要点</script>', rendered)

    @unittest.skipIf(Environment is None, "Jinja2 is not installed")
    def test_public_social_links_follow_intro_and_avatar_precedes_title(self):
        env = Environment(
            loader=FileSystemLoader(MODULE_PATH.parent / "template"),
            autoescape=select_autoescape(["html"]),
        )
        rendered = env.get_template("profile_public.html").render(
            profile={
                "page_title": "ページタイトル",
                "display_name": "表示名",
                "avatar_url": "https://mfu.iori0624.jp/profile/avatar/example.webp",
                "intro_text": "自己紹介テスト",
                "request_notes_text": "撮影依頼テスト",
                "show_sns_links": True,
                "x_url": "https://x.example/",
                "instagram_url": "https://instagram.example/",
                "portfolio_url": "https://portfolio.example/",
            },
            grouped_works=[], qa_items=[],
            social_links=[
                {"label": "X", "url": "https://x.example/", "icon_mode": "bootstrap", "icon_name": "bi-twitter-x", "icon_url": None},
                {"label": "独自", "url": "https://custom.example/", "icon_mode": "custom", "icon_name": None, "icon_url": "https://mfu.iori0624.jp/profile/social-icon/example.webp"},
            ],
        )
        self.assertLess(rendered.index("profile/avatar/example.webp"), rendered.index("ページタイトル</h1>"))
        self.assertLess(rendered.index("自己紹介テスト"), rendered.index("各種SNS　リンク"))
        self.assertLess(rendered.index("各種SNS　リンク"), rendered.index("撮影依頼テスト"))
        self.assertEqual(rendered.count("各種SNS　リンク"), 1)
        self.assertIn("bi bi-twitter-x", rendered)
        self.assertIn("/profile/social-icon/example.webp", rendered)


if __name__ == "__main__":
    unittest.main()
