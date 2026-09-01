import importlib.util
import sys
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "s_u_calendar" / "month_update.py"
SPEC = importlib.util.spec_from_file_location("su_month_update", MODULE_PATH)
month_update = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = month_update
SPEC.loader.exec_module(month_update)


class FakeForm(dict):
    def __init__(self, values, dates):
        super().__init__(values)
        self._dates = dates

    def getlist(self, key):
        return list(self._dates) if key == "dates" else []


def build_month_form(year=2026, month=10):
    last_day = 31
    dates = [date(year, month, day).isoformat() for day in range(1, last_day + 1)]
    values = {}
    for date_key in dates:
        values[f"status__{date_key}"] = "free"
        values[f"label__{date_key}"] = ""
        values[f"is_public__{date_key}"] = "1"
    return FakeForm(values, dates), dates


class SuCalendarMonthSaveTest(unittest.TestCase):
    def test_parse_complete_month_and_preserve_day_order(self):
        form, dates = build_month_form()
        form[f"status__{dates[4]}"] = "busy"
        form[f"label__{dates[4]}"] = "撮影予定"

        updates = month_update.parse_month_updates(form, year=2026, month=10)

        self.assertEqual(len(updates), 31)
        self.assertEqual(updates[4].date_key, "2026-10-05")
        self.assertEqual(updates[4].comparable_values, ("busy", "撮影予定", 1))

    def test_missing_date_is_rejected(self):
        form, dates = build_month_form()
        form._dates = dates[:-1]

        with self.assertRaises(month_update.MonthUpdateValidationError):
            month_update.parse_month_updates(form, year=2026, month=10)

    def test_missing_database_row_uses_ui_defaults(self):
        self.assertEqual(
            month_update.existing_comparable_values(None),
            ("free", "", 1),
        )

    def test_admin_template_uses_one_month_form_without_day_save_buttons(self):
        template = (
            ROOT / "s_u_calendar" / "template" / "calendar_admin.html"
        ).read_text(encoding="utf-8-sig")

        self.assertIn('id="monthDaysForm"', template)
        self.assertIn("この月をまとめて保存", template)
        self.assertIn("未保存の変更：0日", template)
        self.assertNotIn(
            "action=\"{{ url_for('s_u_calendar.admin_upsert_day') }}\"",
            template,
        )


if __name__ == "__main__":
    unittest.main()
