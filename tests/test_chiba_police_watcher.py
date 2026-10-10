"""Regression tests for the Chiba Police daily publication retry."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "mfu_notification_sender" / "chiba_police_watcher.py"
spec = importlib.util.spec_from_file_location("testable_chiba_police_watcher", SCRIPT)
watcher = importlib.util.module_from_spec(spec)
fake_bs4 = types.ModuleType("bs4")
fake_bs4.BeautifulSoup = object
with patch.dict(sys.modules, {"pymysql": types.ModuleType("pymysql"), "bs4": fake_bs4}):
    sys.modules[spec.name] = watcher
    spec.loader.exec_module(watcher)


class FixedDateTime:
    @staticmethod
    def now(_tz):
        return datetime(2026, 10, 10, 10, 0, tzinfo=watcher.JST)


class ChibaPoliceDailyRetryTests(unittest.TestCase):
    def setUp(self):
        self.connection = object()
        self.old_link = watcher.PageLink("https://example.test/orders_prefecture_00001.html", date(2026, 10, 8))
        self.yesterday_link = watcher.PageLink("https://example.test/orders_prefecture_00002.html", date(2026, 10, 9))
        self.patches = [
            patch.object(watcher, "datetime", FixedDateTime),
            patch.object(watcher, "fetch", return_value=(b"page", {})),
            patch.object(watcher, "get_state", return_value=""),
            patch.object(watcher, "has_page_for_date", return_value=False),
            patch.object(watcher, "known_page_urls", return_value=set()),
            patch.object(watcher, "parse_page", side_effect=lambda _raw, day: (day, [])),
            patch.object(watcher, "store_page", return_value=1),
            patch.object(watcher, "deliver_pending", return_value=1),
            patch.object(watcher, "pending_count", return_value=0),
            patch.object(watcher, "set_state"),
            patch.object(watcher, "delete_state"),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    def test_old_latest_page_does_not_complete_today(self):
        with patch.object(watcher, "parse_index", return_value=[self.old_link]):
            watcher.poll(self.connection)
        completed_calls = [
            call for call in watcher.set_state.call_args_list
            if call.args[1] == watcher.STATE_COMPLETED_CYCLE
        ]
        self.assertEqual(completed_calls, [])
        watcher.delete_state.assert_called_once_with(self.connection, watcher.STATE_IN_PROGRESS)

    def test_yesterday_page_completes_today(self):
        with patch.object(watcher, "parse_index", return_value=[self.yesterday_link, self.old_link]):
            watcher.poll(self.connection)
        watcher.set_state.assert_any_call(self.connection, watcher.STATE_COMPLETED_CYCLE, "2026-10-10")

    def test_old_completion_state_is_retried_if_yesterday_missing(self):
        watcher.get_state.side_effect = lambda _connection, key: "2026-10-10" if key == watcher.STATE_COMPLETED_CYCLE else ""
        with patch.object(watcher, "parse_index", return_value=[self.old_link]):
            watcher.poll(self.connection)
        watcher.fetch.assert_called()

    def test_completed_day_with_yesterday_stored_does_not_fetch(self):
        watcher.get_state.return_value = "2026-10-10"
        watcher.has_page_for_date.return_value = True
        self.assertEqual(watcher.poll(self.connection), (0, 0, 0))
        watcher.fetch.assert_not_called()

    def test_known_old_page_checks_index_without_refetching_details(self):
        watcher.known_page_urls.return_value = {self.old_link.url}
        with patch.object(watcher, "parse_index", return_value=[self.old_link]):
            self.assertEqual(watcher.poll(self.connection), (0, 0, 1))
        watcher.fetch.assert_called_once_with(watcher.INDEX_URL)
        watcher.set_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
