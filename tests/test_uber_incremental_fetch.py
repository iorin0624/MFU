from __future__ import annotations

import unittest
from datetime import datetime

from app.records.uber_fetcher import _incremental_detail_required


def complete_delivery(amount: int = 500) -> dict:
    return {
        "activity_type": "delivery",
        "raw_text": "Delivery detail",
        "earnings_yen": amount,
        "points": 1,
        "deliveries": 1,
        "duration_seconds": 600,
        "distance_km": 2.5,
        "merchant_name": "店舗",
        "delivery_address": "配達先",
        "occurred_at": datetime(2026, 9, 6, 12, 0),
    }


class UberIncrementalFetchTest(unittest.TestCase):
    def test_new_detail_is_opened(self):
        self.assertTrue(_incremental_detail_required({"list_amount_yen": 500}, None))

    def test_unchanged_complete_detail_is_not_opened(self):
        self.assertFalse(_incremental_detail_required({"list_amount_yen": 500}, complete_delivery()))

    def test_changed_amount_is_opened_for_tip_update(self):
        self.assertTrue(_incremental_detail_required({"list_amount_yen": 700}, complete_delivery()))

    def test_incomplete_cached_detail_is_reopened(self):
        cached = complete_delivery()
        cached["distance_km"] = None
        self.assertTrue(_incremental_detail_required({"list_amount_yen": 500}, cached))


if __name__ == "__main__":
    unittest.main()
