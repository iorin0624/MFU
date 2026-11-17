import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("profile_formatting", ROOT / "profile" / "formatting.py")
formatting = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(formatting)


class ProfileMarkdownTest(unittest.TestCase):
    def test_markdown_contains_only_public_sections_and_current_data(self):
        profile = {
            "page_title": "いおりん", "display_name": "いおりん写真室",
            "subtitle": "撮影します", "intro_text": "自己紹介<br>次の行",
            "request_notes_text": "予約は https://suc.iori0624.jp/ で確認",
        }
        qa = formatting.parse_profile_qa_text("Q1. 依頼方法は？\n【要点】DMでご連絡ください。\n回答です。")
        result = formatting.render_profile_markdown(
            profile, qa_items=qa,
            social_links=[{"label": "X", "url": "https://x.com/example"}],
            grouped_works=[{"category_name": "ゲーム", "items": [{"item_name": "作品名"}]}],
        )
        self.assertIn("# いおりん\n", result)
        self.assertIn("自己紹介\n次の行", result)
        self.assertIn("- X: https://x.com/example", result)
        self.assertIn("### Q1. 依頼方法は？", result)
        self.assertIn("要点: DMでご連絡ください。", result)
        self.assertIn("- 作品名", result)

    def test_hidden_sections_are_not_included(self):
        result = formatting.render_profile_markdown(
            {"page_title": "公開", "intro_text": "<private>"},
            qa_items=[], social_links=[], grouped_works=[],
        )
        self.assertIn("&lt;private&gt;", result)
        self.assertNotIn("## Q&A", result)
        self.assertNotIn("## 知っている作品", result)
        self.assertNotIn("## 各種SNSリンク", result)


if __name__ == "__main__":
    unittest.main()
