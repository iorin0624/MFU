"""Pure tests for pro/SUC PV extraction; no production database required."""

import importlib.util
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest


spec = importlib.util.spec_from_file_location(
    "pro_suc_pv_analytics", Path(__file__).resolve().parents[1] / "pro_suc_pv" / "analytics.py"
)
analytics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analytics)
local_spec = importlib.util.spec_from_file_location(
    "local_networks", Path(__file__).resolve().parents[1] / "utils" / "local_networks.py"
)
local_networks = importlib.util.module_from_spec(local_spec)
local_spec.loader.exec_module(local_networks)


class PageViewAnalyticsTests(unittest.TestCase):
    def test_country_provider_grouping_counts_all_pvs(self):
        rows = [{"ip": "a", "pv": 3}, {"ip": "b", "pv": 2}, {"ip": "c", "pv": 1}]
        labels = {"a": ("JP", "KDDI"), "b": ("JP", "KDDI"), "c": ("未判定", "未判定")}
        result = analytics.group_cached_origins(rows, "unused", lookup=labels.__getitem__)
        self.assertEqual(result["countries"], [{"country": "JP", "pv": 5}, {"country": "未判定", "pv": 1}])
        self.assertEqual(result["providers"], [
            {"country": "JP", "provider": "KDDI", "pv": 5},
            {"country": "未判定", "provider": "未判定", "pv": 1},
        ])
        self.assertEqual((result["origin_total"], result["known_country"], result["known_provider"]), (6, 5, 5))

    def test_expired_whois_cache_remains_available_for_history(self):
        with tempfile.TemporaryDirectory() as folder:
            ip = "203.0.113.9"
            name = hashlib.md5(ip.encode()).hexdigest() + ".json"
            (Path(folder) / name).write_text(json.dumps({
                "country": "Japan", "display_name": "Sample Net", "expires_at": 1,
            }), encoding="utf-8")
            self.assertEqual(analytics._cached_origin(ip, folder, 1), ("JP", "Sample Net"))

    def test_local_networks_match_existing_ranges(self):
        for value in ("127.0.0.1", "::1", "192.168.103.21",
                      "fdba:577c:d08:221a:103::1", "fdba:577c:d08:221a:103::41",
                      "2404:7a81:bc40:2a00::1234", "2404:7a81:8ac1:1000::abcd"):
            self.assertTrue(local_networks.is_local_ip(value), value)
        for value in ("192.168.104.21", "fdba:577c:d08:221b:103::1",
                      "2404:7a81:bc40:2a01::1", "203.0.113.2"):
            self.assertFalse(local_networks.is_local_ip(value), value)
        sql, values = local_networks.sql_local_ip_predicate()
        self.assertEqual(sql.count("%s"), len(values))
        self.assertIn("192.168.103.%", values)
        self.assertIn("fdba:577c:d08:221a:%", values)
        self.assertIn("2404:7a81:bc40:2a00:%", values)

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
