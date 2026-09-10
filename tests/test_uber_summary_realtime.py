from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UberSummaryRealtimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_source = (ROOT / "__init__.py").read_text(encoding="utf-8")
        cls.bp_source = (ROOT / "records" / "bp.py").read_text(encoding="utf-8")
        cls.fetcher_source = (ROOT / "records" / "uber_fetcher.py").read_text(encoding="utf-8")
        cls.template_source = (
            ROOT / "records" / "templates" / "records" / "uber" / "list.html"
        ).read_text(encoding="utf-8")

    def test_admin_socket_has_authenticated_summary_subscription(self):
        self.assertIn(
            '@socketio.on("uber_summary_subscribe", namespace="/admin-system")',
            self.app_source,
        )
        self.assertIn('join_room("uber-summary")', self.app_source)

    def test_fetcher_emits_for_inserted_and_updated_details(self):
        self.assertIn('if outcome in {"inserted", "updated"}:', self.fetcher_source)
        self.assertIn("emit_uber_summary_updated({", self.fetcher_source)
        self.assertIn('"changed_dates":', self.fetcher_source)

    def test_summary_json_endpoint_reuses_server_calculation(self):
        self.assertIn('@records_bp.get("/uber/activity-summary")', self.bp_source)
        self.assertIn(
            "_present_uber_activity_summary(activity_range_summary(date_from, date_to))",
            self.bp_source,
        )

    def test_frontend_uses_socket_with_polling_and_resume_fallbacks(self):
        self.assertIn("summarySocket.on('uber_summary_updated', refreshSummary)", self.template_source)
        self.assertIn("window.setInterval(refreshSummary, 30000)", self.template_source)
        self.assertIn("document.addEventListener('visibilitychange'", self.template_source)
        self.assertIn("window.addEventListener('pageshow', refreshSummary)", self.template_source)


if __name__ == "__main__":
    unittest.main()
