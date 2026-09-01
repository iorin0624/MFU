import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SuCalendarScheduleListTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = (ROOT / "s_u_calendar" / "routes.py").read_text(
            encoding="utf-8-sig"
        )
        cls.template = (
            ROOT / "s_u_calendar" / "template" / "calendar_month.html"
        ).read_text(encoding="utf-8-sig")

    def test_month_route_passes_only_busy_days_to_template(self):
        self.assertIn(
            'busy_days = [item for item in days if item["status"] == "busy"]',
            self.routes,
        )
        self.assertIn("busy_days=busy_days", self.routes)

    def test_public_page_contains_responsive_schedule_list(self):
        self.assertIn('id="busyScheduleList"', self.template)
        self.assertIn("予定のある日", self.template)
        self.assertIn("{% for it in busy_days %}", self.template)
        self.assertIn("この月の予定はありません。", self.template)
        self.assertIn("grid-template-columns:repeat(2", self.template)
        self.assertIn("grid-template-columns:1fr", self.template)

    def test_lightweight_view_is_retired_without_breaking_old_url(self):
        self.assertNotIn("軽量表示（直近30日）", self.template)
        self.assertIn("Retired lightweight view", self.routes)
        self.assertIn('"s_u_calendar.calendar_month"', self.routes)
        self.assertFalse(
            (ROOT / "s_u_calendar" / "template" / "calendar_mini.html").exists()
        )


if __name__ == "__main__":
    unittest.main()
