import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from s_u_calendar.baikal_sync import expand_days, _month_horizon
except ImportError:
    expand_days = None


@unittest.skipIf(expand_days is None, "CalDAV calendar dependencies are not installed")
class BaikalCalendarTests(unittest.TestCase):
    def test_all_day_transparent_event_is_busy(self):
        ics = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:a@example.test",
            "DTSTART;VALUE=DATE:20270307", "DTEND;VALUE=DATE:20270308",
            "TRANSP:TRANSPARENT", "SUMMARY:Private test", "END:VEVENT", "END:VCALENDAR", "",
        ])
        self.assertEqual(expand_days([ics], date(2027, 3, 1), date(2027, 3, 31)),
                         {"2027-03-07"})

    def test_recurrence_and_exception(self):
        ics = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:b@example.test",
            "DTSTART;VALUE=DATE:20270301", "DTEND;VALUE=DATE:20270302",
            "RRULE:FREQ=DAILY;COUNT=3", "EXDATE;VALUE=DATE:20270302",
            "END:VEVENT", "END:VCALENDAR", "",
        ])
        self.assertEqual(expand_days([ics], date(2027, 3, 1), date(2027, 3, 31)),
                         {"2027-03-01", "2027-03-03"})

    def test_midnight_end_is_exclusive(self):
        ics = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:c@example.test",
            "DTSTART:20270307T150000Z", "DTEND:20270308T150000Z",
            "END:VEVENT", "END:VCALENDAR", "",
        ])
        self.assertEqual(expand_days([ics], date(2027, 3, 7), date(2027, 3, 10)),
                         {"2027-03-08"})

    def test_holiday_names_separate_from_busy(self):
        ics = "\r\n".join([
            "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:d@example.test",
            "DTSTART;VALUE=DATE:20270321", "DTEND;VALUE=DATE:20270322",
            "SUMMARY:春分の日", "END:VEVENT", "END:VCALENDAR", "",
        ])
        self.assertEqual(expand_days([ics], date(2027, 3, 1), date(2027, 3, 31), holidays=True),
                         {"2027-03-21": "春分の日"})

    def test_twelve_month_horizon(self):
        self.assertEqual(_month_horizon(date(2026, 11, 13)),
                         (date(2026, 11, 1), date(2027, 10, 31)))
        self.assertEqual(_month_horizon(date(2027, 1, 1)),
                         (date(2027, 1, 1), date(2027, 12, 31)))

    def test_expanded_range_refreshes_even_when_tokens_are_unchanged(self):
        from s_u_calendar.baikal_sync import synchronize

        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            ("unchanged",), ("unchanged",), ("2026-11-13",),
            ("2026-11-01..2027-04-30",),
        ]
        cursor.fetchall.side_effect = [[], []]
        db = MagicMock()
        db.cursor.return_value = cursor
        with patch("s_u_calendar.baikal_sync._password", return_value="test"), \
             patch("s_u_calendar.baikal_sync.collection_token", return_value="unchanged"), \
             patch("s_u_calendar.baikal_sync.read_range", side_effect=[set(), {}]) as read:
            result = synchronize(db, today=date(2026, 11, 13))
        self.assertTrue(result["changed"])
        self.assertEqual(result["range"], "2026-11-01..2027-10-31")
        self.assertEqual(read.call_args_list[0].args[:2],
                         (date(2026, 11, 1), date(2027, 10, 31)))
        db.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
