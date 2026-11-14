"""Pure tests for pro/SUC PV extraction; no production database required."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


spec = importlib.util.spec_from_file_location(
    "pro_suc_pv_analytics", Path(__file__).resolve().parents[1] / "pro_suc_pv" / "analytics.py"
)
analytics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analytics)


class PageViewAnalyticsTests(unittest.TestCase):
    def test_only_public_host_page_success_is_counted(self):
        request = SimpleNamespace(
            host="pro.iori0624.jp", method="GET", path="/profile",
            args=SimpleNamespace(get=lambda k: None, getlist=lambda k: []),
            remote_addr="203.0.113.2", headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130"},
        )
        response = SimpleNamespace(status_code=200)
        self.assertIsNotNone(analytics.event_for_request(request, response, {}))
        request.host = "mfu.iori0624.jp"
        self.assertIsNone(analytics.event_for_request(request, response, {}))
        request.host = "pro.iori0624.jp"
        response.status_code = 302
        self.assertIsNone(analytics.event_for_request(request, response, {}))
        response.status_code = 200
        self.assertIsNone(analytics.event_for_request(request, response, {}, "[INAPP_WARNING]"))

    def test_apache_combined_profile(self):
        line = ('203.0.113.4 - - [14/Nov/2026:12:00:00 +0900] '
                '"GET /?src=poster HTTP/1.1" 200 1200 "https://search.example/path" '
                '"Mozilla/5.0 (iPhone; CPU iPhone OS 26_0) Safari/604.1"')
        event = analytics.parse_apache_page_view(line, "pro")
        self.assertEqual(event["src"], "poster")
        self.assertEqual(event["source_kind"], "src")
        self.assertEqual(event["device"], "スマートフォン")
        self.assertEqual(event["viewed_at"].hour, 12)

    def test_apache_vhost_calendar_month(self):
        line = ('suc.iori0624.jp:443 203.0.113.5 - - [14/Nov/2026:12:01:00 +0900] '
                '"GET /?year=2027&month=3 HTTP/2.0" 200 1234 "-" '
                '"Mozilla/5.0 (Macintosh) Chrome/130"')
        event = analytics.parse_apache_page_view(line, "suc")
        self.assertEqual(event["calendar_month"], "2027-03")
        self.assertEqual(event["source_kind"], "direct")
        self.assertIsNone(analytics.parse_apache_page_view(line.replace(' 200 ', ' 404 '), "suc"))

    def test_legacy_source_and_bot(self):
        self.assertEqual(analytics.legacy_source('[TRAFFIC_SOURCE] src="Instagram DM" GET /profile 200'), "Instagram DM")
        event = analytics.make_event(
            site="pro", viewed_at=analytics.datetime.now(analytics.JST), ip="203.0.113.5",
            ua="Googlebot/2.1", referer="", origin="legacy_db",
        )
        self.assertEqual(event["is_bot"], 1)


if __name__ == "__main__":
    unittest.main()
