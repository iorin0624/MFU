from __future__ import annotations

import unittest
from datetime import date

from app.records.uber_notifications import build_uber_summary_payload


class UberSummaryNotificationTest(unittest.TestCase):
    def setUp(self):
        self.summary = {
            "deliveries": 2,
            "net_yen": 1000,
            "promo_yen": 200,
            "other_yen": 50,
            "tip_yen": 30,
            "cash_collected_yen": 500,
            "duration_seconds": 1800,
            "distance_km": 5,
            "total_per_delivery_average": 640,
            "total_per_delivery_median": 630,
            "total_per_hour_average": 2560,
            "total_per_hour_median": 2500,
            "total_per_km_average": 256,
            "total_per_km_median": 250,
            "net_per_delivery_median": 500,
            "net_per_hour_median": 2000,
            "net_per_km_median": 200,
        }

    def test_payload_matches_web_summary_sections(self):
        payload = build_uber_summary_payload(
            date(2026, 9, 8), date(2026, 9, 8), summary=self.summary, inserted_count=2
        )

        self.assertEqual(len(payload["embeds"]), 4)
        overview = {field["name"]: field["value"] for field in payload["embeds"][0]["fields"]}
        self.assertEqual(overview["売上合計"], "¥1,280")
        self.assertEqual(overview["配達＋クエスト合計"], "¥1,200")
        self.assertEqual(overview["その他・調整金"], "¥50")
        self.assertIn("新規取得：2件", payload["embeds"][0]["description"])

    def test_test_payload_is_clearly_labelled(self):
        payload = build_uber_summary_payload(
            date(2026, 9, 1), date(2026, 9, 8), summary=self.summary, test=True
        )

        self.assertEqual(payload["embeds"][0]["title"], "【テスト】Uber売上途中集計")
        self.assertIn("2026年9月1日～2026年9月8日", payload["embeds"][0]["description"])


if __name__ == "__main__":
    unittest.main()
