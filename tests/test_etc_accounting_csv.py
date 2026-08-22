from __future__ import annotations

import csv
import io
import unittest
from datetime import datetime
from unittest.mock import patch

from app.etc_accounting.csv_export import CSV_HEADERS, render_csv


class ETCAccountingCSVTest(unittest.TestCase):
    def test_csv_has_excel_encoding_crlf_columns_and_chronological_order(self):
        records = [
            {
                "id": 2,
                "entry_at": datetime(2026, 7, 26, 23, 55),
                "exit_at": datetime(2026, 7, 28, 0, 0),
                "entry_ic": "入口B",
                "exit_ic": "出口B",
                "amount": 1080,
                "remarks": '深夜割引, "確認"',
                "tollgate_operator_name": "東日本高速道路株式会社",
                "tollgate_match_status": "matched",
            },
            {
                "id": 1,
                "entry_at": datetime(2026, 7, 25, 16, 56),
                "exit_at": datetime(2026, 7, 25, 17, 27),
                "entry_ic": "入口A",
                "exit_ic": "出口A",
                "amount": 750,
                "remarks": "確定",
                "tollgate_operator_name": None,
                "tollgate_match_status": "unmatched",
            },
        ]

        payload = render_csv(records)

        self.assertTrue(payload.startswith(b"\xef\xbb\xbf"))
        decoded = payload.decode("utf-8-sig")
        self.assertNotIn("\n", decoded.replace("\r\n", ""))
        rows = list(csv.reader(io.StringIO(decoded, newline="")))
        self.assertEqual(tuple(rows[0]), CSV_HEADERS)
        self.assertEqual(rows[1][0], "2026/07/25 16:56")
        self.assertEqual(rows[1][4], "0:31")
        self.assertEqual(rows[1][5], "未特定")
        self.assertEqual(rows[1][6], "750")
        self.assertEqual(rows[2][0], "2026/07/26 23:55")
        self.assertEqual(rows[2][4], "1日 0:05")
        self.assertEqual(rows[2][7], '深夜割引, "確認"')

    def test_csv_fills_missing_labels_and_protects_formulas(self):
        payload = render_csv(
            [
                {
                    "id": 1,
                    "used_at": datetime(2026, 7, 25, 16, 56),
                    "entry_at": None,
                    "exit_at": None,
                    "entry_ic": "=HYPERLINK(\"https://example.invalid\")",
                    "exit_ic": "",
                    "amount": None,
                    "remarks": "@SUM(1+1)",
                    "tollgate_operator_name": "",
                    "tollgate_match_status": "unmatched",
                }
            ]
        )
        rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"), newline="")))
        self.assertTrue(rows[1][1].startswith("'="))
        self.assertEqual(rows[1][3], "出口記録なし")
        self.assertEqual(rows[1][4], "")
        self.assertEqual(rows[1][5], "未特定")
        self.assertEqual(rows[1][6], "")
        self.assertTrue(rows[1][7].startswith("'@"))

    def test_export_route_uses_current_filters_without_mutation(self):
        from app import app

        with (
            patch("app.etc_accounting.routes.ensure_schema"),
            patch("app.etc_accounting.routes.list_tollgate_operators", return_value=["会社A"]),
            patch("app.etc_accounting.routes.list_records", return_value=[]) as list_records,
            app.test_client() as client,
        ):
            with client.session_transaction() as flask_session:
                flask_session["user"] = "admin"
            response = client.get(
                "/etc-accounting/export.csv"
                "?scope=filtered&status=registered&date_from=2026-07-01"
                "&date_to=2026-07-31&operator=会社A"
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
        self.assertIn("attachment; filename=", response.headers["Content-Disposition"])
        list_records.assert_called_once_with(
            status="registered",
            limit=None,
            date_from=datetime(2026, 7, 1).date(),
            date_to=datetime(2026, 7, 31).date(),
            operator_name="会社A",
        )

    def test_export_route_all_ignores_filters(self):
        from app import app

        with (
            patch("app.etc_accounting.routes.ensure_schema"),
            patch("app.etc_accounting.routes.list_records", return_value=[]) as list_records,
            app.test_client() as client,
        ):
            with client.session_transaction() as flask_session:
                flask_session["user"] = "admin"
            response = client.get(
                "/etc-accounting/export.csv"
                "?scope=all&status=error&date_from=2026-07-01&operator=会社A"
            )

        self.assertEqual(response.status_code, 200)
        list_records.assert_called_once_with(
            status="",
            limit=None,
            date_from=None,
            date_to=None,
            operator_name="",
        )


if __name__ == "__main__":
    unittest.main()
