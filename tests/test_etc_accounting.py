from datetime import datetime
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, Mock, patch

from flask import render_template

from app.etc_accounting import credentials as etc_credentials
from app.etc_accounting import browser_session as etc_browser_session
from app.etc_accounting.browser_session import ETCMaintenanceError, ETCTargetPage
from app.etc_accounting.batch import registration_eligibility, run_batch_job
from app.etc_accounting.fetcher import _discard_replaced_pdf, _pdf_metadata, fetch_month, scheduled_months
from app.etc_accounting import fetch_cli
from app.etc_accounting import manual_jobs
from app.etc_accounting import repository as etc_repository
from app.etc_accounting.freee_sync import (
    _deal_payload,
    _deal_update_payload,
    _description,
    _ensure_receipt_for_update,
    _parse_invoice_registration_number,
    _update_receipt_invoice_metadata,
    _upload_pdf,
    register_record,
    update_registered_record,
)
from app.etc_accounting.parser import ETCAuthenticationRequired, is_provisional_record, parse_statement_page
from app.etc_accounting.pdf_metadata import parse_invoice_issuer_name
from app.etc_accounting.presentation import travel_duration_minutes
from app.etc_accounting.notifications import (
    _discord_batches,
    dispatch_pending_new_record_notifications,
    send_test_notification,
)
from app.etc_accounting.invoice_issuers import INVOICE_ISSUERS, canonical_issuer_name
from app.etc_accounting.routes import (
    _format_travel_duration,
    _month_options,
    _normalize_status_filter,
    _parse_filter_date,
    _parse_filter_month,
    _sort_batch_records,
)
from app.etc_accounting.tollgate_reference import (
    _reference_lookup,
    normalize_tollgate_name,
    resolve_exit_tollgate,
)
from app.freee_api import services as freee_services


STATEMENT_HTML = """
<html><body><form name="frm">
  <button onclick="submitPage('frm','/etc/R?nextfunc=1013100000&pageNo=2')">2P</button>
  <input type="hidden" name="p" value="token-value">
  <table><tr>
    <td><input type="checkbox" name="hakkoMeisai" value="202606010625-example"></td>
    <td><table><tr><td>市原</td><td>26/06/01<br>06:25<br>千葉西</td></tr></table></td>
    <td>750</td><td>0 750</td><td>5 ********2159</td><td>確定</td>
  </tr></table>
</form></body></html>
"""


class ETCAccountingTest(unittest.TestCase):
    def test_format_travel_duration(self):
        self.assertEqual(
            _format_travel_duration(
                datetime(2026, 7, 25, 16, 56),
                datetime(2026, 7, 25, 17, 27),
            ),
            "0:31",
        )
        self.assertEqual(
            _format_travel_duration(
                datetime(2026, 7, 25, 16, 56),
                datetime(2026, 7, 26, 17, 1),
            ),
            "1日 0:05",
        )

    def test_format_travel_duration_ignores_incomplete_or_invalid_times(self):
        start = datetime(2026, 7, 25, 16, 56)
        self.assertIsNone(_format_travel_duration(start, None))
        self.assertIsNone(_format_travel_duration(None, start))
        self.assertIsNone(
            _format_travel_duration(
                start,
                datetime(2026, 7, 25, 16, 55),
            )
        )
        self.assertEqual(
            travel_duration_minutes(
                start,
                datetime(2026, 7, 26, 17, 1),
            ),
            1445,
        )

    def test_index_template_exposes_travel_aggregation_details(self):
        from app import app

        records = [
            {
                "id": 1,
                "used_at": datetime(2026, 7, 25, 16, 56),
                "entry_at": datetime(2026, 7, 25, 16, 56),
                "exit_at": datetime(2026, 7, 25, 17, 27),
                "entry_ic": "湾岸市川",
                "exit_ic": "市原",
                "travel_duration": "0:31",
                "travel_duration_minutes": 31,
                "amount": 1080,
                "status": "pending",
                "is_provisional": True,
            },
            {
                "id": 2,
                "used_at": datetime(2026, 7, 26, 17, 1),
                "entry_at": datetime(2026, 7, 25, 16, 56),
                "exit_at": datetime(2026, 7, 26, 17, 1),
                "entry_ic": "入口IC",
                "exit_ic": "出口IC",
                "travel_duration": "1日 0:05",
                "travel_duration_minutes": 1445,
                "amount": 2000,
                "status": "registered",
                "is_provisional": False,
            },
        ]
        with app.test_request_context("/etc-accounting/"):
            rendered = render_template(
                "etc_accounting/index.html",
                records=records,
                filtered_record_count=2,
                filtered_total_amount=3080,
                summary_period_label="全期間",
                runs=[],
                selected_status="",
                freee_connected=True,
                settings={},
                is_admin=False,
                batch_jobs=[],
                month_options=[],
                scheduled_fetch_state={},
                selected_month="",
                selected_operator="",
                operator_options=[],
                csrf_token="test-token",
            )

        self.assertIn('id="travelAggregationToggle"', rendered)
        self.assertIn('id="travelSummaryDetails"', rendered)
        self.assertIn('data-duration-minutes="31"', rendered)
        self.assertIn('data-entry-ic="湾岸市川"', rendered)
        self.assertIn('data-entry-display="2026/07/25 16:56"', rendered)
        self.assertIn('data-exit-display="2026/07/25 17:27"', rendered)
        self.assertIn('data-exit-sort-key="202607251727"', rendered)
        self.assertIn('data-duration-minutes="1445"', rendered)
        self.assertIn("選択範囲の経過時間", rendered)
        self.assertIn("途中の空き時間を含みます", rendered)
        self.assertIn("compactDateToMinutes(latest.dataset.exitSortKey)", rendered)
        self.assertIn("2件", rendered)
        self.assertIn("¥3,080", rendered)

    def test_fetch_month_downloads_provisional_pdf_but_keeps_freee_blocked(self):
        record = {
            "transaction_key": "provisional-key",
            "statement_month": "202607",
            "used_at": datetime(2026, 7, 22, 2, 41),
            "entry_ic": "Mobara-Nagara Smart",
            "exit_ic": "Ichihara",
            "amount": 870,
            "vehicle_type": "5",
            "card_mask": "********2159",
            "remarks": "\u78ba\u8a8d\u4e2d \u6df1\u591c\u5272\u5f15",
        }
        page = Mock(records=[record], page_numbers=[1], form_token="form-token")
        browser = MagicMock()
        browser.__enter__.return_value = browser
        browser.__exit__.return_value = False
        stored = {**record, "id": 793, "pdf_path": None, "_details_changed": False}
        lock = Mock()

        with (
            patch("app.etc_accounting.fetcher.acquire_fetch_lock", return_value=lock),
            patch("app.etc_accounting.fetcher.release_fetch_lock") as release_lock,
            patch("app.etc_accounting.fetcher.start_run", return_value=1),
            patch("app.etc_accounting.fetcher.finish_run") as finish_run,
            patch("app.etc_accounting.fetcher.etc_browser_lock", return_value=MagicMock()),
            patch("app.etc_accounting.fetcher.ETCTargetPage", return_value=browser),
            patch("app.etc_accounting.fetcher.parse_statement_page", return_value=page),
            patch("app.etc_accounting.fetcher.upsert_record", return_value=stored),
            patch(
                "app.etc_accounting.fetcher.reconcile_source_records",
                return_value={"checked": 1, "present": 1, "missing": 0, "newly_deleted": 0, "already_deleted": 0},
            ) as reconcile,
            patch(
                "app.etc_accounting.fetcher.enrich_record_tollgate",
                return_value={"status": "matched", "operator_name": "東日本高速道路株式会社"},
            ),
            patch("app.etc_accounting.fetcher._download_pdf", return_value=b"%PDF-1.4 provisional") as download,
            patch("app.etc_accounting.fetcher._stage_pdf_bytes", return_value=(Path("/tmp/provisional.pdf"), MagicMock(), "sha256")),
            patch("app.etc_accounting.fetcher._pdf_metadata", return_value={"registration_number": None, "issuer_name": None}),
            patch("app.etc_accounting.fetcher.save_pdf") as save,
            patch("app.etc_accounting.fetcher._discard_replaced_pdf"),
        ):
            result = fetch_month("202607")

        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(result["skipped"], 0)
        download.assert_called_once_with(browser, "form-token", record)
        save.assert_called_once()
        finish_run.assert_called_once_with(1, status="success", found=1, downloaded=1, skipped=0)
        reconcile.assert_called_once_with("202607", {"provisional-key"})
        release_lock.assert_called_once_with(lock)
        self.assertEqual(registration_eligibility(stored, company_id=1, mapping={}, check_pdf_file=False), (False, "\u6599\u91d1\u78ba\u8a8d\u4e2d"))

    def test_provisional_pdf_can_be_saved_before_invoice_metadata_is_final(self):
        record = {"transaction_key": "provisional-key", "remarks": "\u78ba\u8a8d\u4e2d"}
        with patch("app.etc_accounting.fetcher.extract_pdf_metadata", side_effect=RuntimeError("not final")):
            metadata = _pdf_metadata(Path("/tmp/provisional.pdf"), record)
        self.assertEqual(metadata, {"registration_number": None, "issuer_name": None})

    def test_confirmed_pdf_requires_invoice_metadata(self):
        record = {"transaction_key": "confirmed-key", "remarks": "\u78ba\u5b9a"}
        with (
            patch("app.etc_accounting.fetcher.extract_pdf_metadata", side_effect=RuntimeError("missing")),
            self.assertRaisesRegex(RuntimeError, "missing"),
        ):
            _pdf_metadata(Path("/tmp/confirmed.pdf"), record)

    def test_replaced_pdf_is_removed_only_inside_etc_storage(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_pdf = root / "old.pdf"
            new_pdf = root / "new.pdf"
            outside_pdf = root.parent / "outside.pdf"
            old_pdf.write_bytes(b"old")
            new_pdf.write_bytes(b"new")
            outside_pdf.write_bytes(b"outside")
            try:
                with patch("app.etc_accounting.fetcher.PDF_ROOT", root):
                    self.assertTrue(_discard_replaced_pdf(str(old_pdf), new_pdf))
                    self.assertFalse(_discard_replaced_pdf(str(outside_pdf), new_pdf))
                self.assertFalse(old_pdf.exists())
                self.assertTrue(new_pdf.exists())
                self.assertTrue(outside_pdf.exists())
            finally:
                outside_pdf.unlink(missing_ok=True)

    def test_upsert_rekeys_matching_provisional_record_without_creating_notification(self):
        record = {
            "transaction_key": "new-key",
            "statement_month": "202607",
            "used_at": datetime(2026, 7, 20, 13, 57),
            "entry_ic": "Soga-minami",
            "exit_ic": "Ichihara",
            "amount": 440,
            "vehicle_type": "5",
            "card_mask": "********2159",
            "remarks": "\u78ba\u8a8d\u4e2d",
        }
        previous = {**record, "id": 30, "transaction_key": "old-key"}
        stored = {**record, "id": 30, "status": "pending"}
        cursor = Mock()
        cursor.fetchone.side_effect = [None, stored]
        cursor.fetchall.return_value = [previous]
        db = Mock()
        db.cursor.return_value = cursor

        with (
            patch.object(etc_repository, "ensure_schema"),
            patch.object(etc_repository, "get_db", return_value=db),
        ):
            result = etc_repository.upsert_record(record)

        executed = cursor.execute.call_args_list
        rekey_calls = [
            call for call in executed
            if "UPDATE etc_freee_records SET transaction_key=%s WHERE id=%s" in call.args[0]
        ]
        notification_calls = [
            call for call in executed
            if "INSERT IGNORE INTO etc_record_notifications" in call.args[0]
        ]
        self.assertEqual(rekey_calls[0].args[1], ("new-key", 30))
        self.assertFalse(notification_calls)
        self.assertFalse(result["_is_new"])
        db.commit.assert_called_once()

    def test_upsert_queues_notification_when_provisional_charge_becomes_final(self):
        previous = {
            "id": 31,
            "transaction_key": "same-key",
            "statement_month": "202607",
            "used_at": datetime(2026, 7, 20, 13, 57),
            "entry_at": datetime(2026, 7, 20, 13, 57),
            "exit_at": datetime(2026, 7, 20, 14, 4),
            "entry_ic": "蘇我南",
            "exit_ic": "市原",
            "amount": 440,
            "vehicle_type": "5",
            "card_mask": "********2159",
            "remarks": "確認中",
            "source_state": "present",
        }
        record = {**previous, "remarks": "確定"}
        stored = {**record, "status": "pending"}
        cursor = Mock()
        cursor.fetchone.side_effect = [previous, stored]
        db = Mock()
        db.cursor.return_value = cursor

        with (
            patch.object(etc_repository, "ensure_schema"),
            patch.object(etc_repository, "get_db", return_value=db),
        ):
            result = etc_repository.upsert_record(record)

        notification_calls = [
            call for call in cursor.execute.call_args_list
            if "INSERT INTO etc_record_notifications" in call.args[0]
        ]
        self.assertTrue(result["_became_final"])
        self.assertEqual(notification_calls[0].args[1][0:2], (31, "finalized"))
        metadata_reset_calls = [
            call for call in cursor.execute.call_args_list
            if "SET invoice_registration_number=NULL" in call.args[0]
        ]
        self.assertEqual(metadata_reset_calls[0].args[1], (31,))

    def test_provisional_rekey_requires_one_unambiguous_candidate(self):
        cursor = Mock()
        cursor.fetchall.return_value = [{"id": 1}, {"id": 2}]
        record = {
            "statement_month": "202607",
            "used_at": datetime(2026, 7, 20, 13, 57),
            "entry_ic": "Soga-minami",
            "exit_ic": "Ichihara",
            "amount": 440,
            "vehicle_type": "5",
            "card_mask": "********2159",
        }

        candidate = etc_repository._find_provisional_rekey_candidate(cursor, record)

        self.assertIsNone(candidate)
        query = cursor.execute.call_args.args[0]
        self.assertNotIn("AND amount=%s", query)

    def test_source_reconciliation_soft_deletes_after_two_successful_misses(self):
        cursor = Mock()
        cursor.fetchall.return_value = [
            {
                "id": 1,
                "transaction_key": "seen-key",
                "source_state": "present",
                "source_missing_count": 0,
            },
            {
                "id": 2,
                "transaction_key": "first-miss",
                "source_state": "present",
                "source_missing_count": 0,
            },
            {
                "id": 3,
                "transaction_key": "second-miss",
                "source_state": "missing",
                "source_missing_count": 1,
            },
        ]
        db = Mock()
        db.cursor.return_value = cursor
        with (
            patch.object(etc_repository, "ensure_schema"),
            patch.object(etc_repository, "get_db", return_value=db),
        ):
            result = etc_repository.reconcile_source_records("202607", {"seen-key"})

        updates = cursor.executemany.call_args.args[1]
        self.assertEqual(updates[0][0:2], ("missing", 1))
        self.assertEqual(updates[1][0:2], ("deleted", 2))
        self.assertEqual(result["present"], 1)
        self.assertEqual(result["missing"], 1)
        self.assertEqual(result["newly_deleted"], 1)
        notification_calls = [
            call for call in cursor.execute.call_args_list
            if "INSERT INTO etc_record_notifications" in call.args[0]
        ]
        self.assertEqual(notification_calls[0].args[1][0:2], (3, "source_deleted"))
        db.commit.assert_called_once()

    def test_deleted_source_record_is_not_freee_eligible(self):
        allowed, reason = registration_eligibility(
            {"source_state": "deleted", "status": "pending"},
            company_id=1,
            mapping={"partner_id": 1, "item_id": 2},
            check_pdf_file=False,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "照会サービスから削除済み")

    def test_date_filter_accepts_iso_date_and_rejects_invalid_value(self):
        self.assertEqual(_parse_filter_date("2026-06-20"), datetime(2026, 6, 20).date())
        self.assertIsNone(_parse_filter_date(""))
        self.assertIsNone(_parse_filter_date("2026-99-99"))
        self.assertEqual(_normalize_status_filter("deleted"), "deleted")

    def test_month_filter_returns_first_and_last_day(self):
        self.assertEqual(
            _parse_filter_month("2026-02"),
            (datetime(2026, 2, 1).date(), datetime(2026, 2, 28).date()),
        )
        self.assertEqual(
            _parse_filter_month("2028-02"),
            (datetime(2028, 2, 1).date(), datetime(2028, 2, 29).date()),
        )
        self.assertEqual(_parse_filter_month(""), (None, None))
        self.assertEqual(_parse_filter_month("2026-13"), (None, None))

    def test_deleted_source_record_renders_in_separate_read_only_list(self):
        from app import app

        record = {
            "id": 93,
            "used_at": datetime(2026, 7, 20, 21, 27),
            "entry_at": datetime(2026, 7, 20, 21, 27),
            "exit_at": datetime(2026, 7, 20, 21, 27),
            "entry_ic": "海ほたる",
            "exit_ic": "木更津金田第一",
            "amount": 0,
            "remarks": "確認中",
            "card_mask": "********2159",
            "status": "pending",
            "source_state": "deleted",
            "source_deleted": True,
            "source_deleted_at": datetime(2026, 8, 6, 20, 5),
            "is_provisional": True,
            "travel_duration": "0:00",
            "travel_duration_minutes": 0,
            "batch_eligible": False,
            "batch_reason": "照会サービスから削除済み",
            "registration_mapping_ready": False,
            "pdf_path": "/tmp/example.pdf",
        }
        with app.test_request_context("/etc-accounting/?status=deleted"):
            rendered = render_template(
                "etc_accounting/index.html",
                records=[record],
                filtered_record_count=1,
                filtered_total_amount=0,
                summary_period_label="全期間",
                runs=[],
                selected_status="deleted",
                freee_connected=True,
                settings={},
                is_admin=True,
                batch_jobs=[],
                month_options=[],
                scheduled_fetch_state={},
                selected_month="",
                selected_operator="",
                operator_options=[],
                csrf_token="test-token",
            )

        self.assertIn("削除された明細", rendered)
        self.assertIn("ETC利用照会サービスから削除済み", rendered)
        self.assertIn("海ほたる", rendered)
        self.assertNotIn('id="etcBatchSelectionForm"', rendered)
        self.assertNotIn("freeeへ登録", rendered)

    def test_registration_settings_always_include_all_catalog_issuers(self):
        cursor = Mock()
        cursor.fetchall.side_effect = [
            [{
                "registration_number": "T9010001095716",
                "issuer_name": "NEXCO東日本お客さまセンター",
                "record_count": 8,
                "unregistered_count": 3,
            }],
            [{
                "id": 1,
                "company_id": 1,
                "registration_number": "T9010001095716",
                "partner_id": 10,
                "partner_name": "東日本高速道路株式会社",
                "item_id": 20,
                "item_name": "高速道路",
            }],
        ]
        db = Mock()
        db.cursor.return_value = cursor
        with (
            patch.object(etc_repository, "ensure_schema"),
            patch.object(etc_repository, "get_db", return_value=db),
        ):
            rows = etc_repository.list_registration_mappings(1)

        self.assertEqual(len(rows), 22)
        self.assertEqual(rows[0]["issuer_name"], "東日本高速道路株式会社")
        self.assertEqual(rows[0]["record_count"], 8)
        self.assertTrue(rows[0]["configured"])
        self.assertEqual(rows[1]["record_count"], 0)
        self.assertFalse(rows[1]["configured"])

    def test_registered_record_filters_are_combined_by_tollgate_operator(self):
        cursor = Mock()
        cursor.fetchall.return_value = [{
            "id": 1,
            "invoice_registration_number": "T9010001095716",
            "invoice_issuer_name": "NEXCO東日本お客さまセンター",
        }]
        db = Mock()
        db.cursor.return_value = cursor
        date_from = datetime(2026, 6, 1).date()
        date_to = datetime(2026, 6, 30).date()
        with (
            patch.object(etc_repository, "ensure_schema"),
            patch.object(etc_repository, "get_db", return_value=db),
        ):
            rows = etc_repository.list_records(
                status="registered",
                limit=None,
                date_from=date_from,
                date_to=date_to,
                operator_name="東日本高速道路株式会社",
            )

        query, params = cursor.execute.call_args.args
        self.assertNotIn("LIMIT", query)
        self.assertIn("status=%s", query)
        self.assertIn("used_at >= %s", query)
        self.assertIn("used_at < DATE_ADD(%s, INTERVAL 1 DAY)", query)
        self.assertIn("tollgate_operator_name=%s", query)
        self.assertEqual(
            params,
            ("registered", date_from, date_to, "東日本高速道路株式会社"),
        )
        self.assertEqual(rows[0]["invoice_issuer_name"], "東日本高速道路株式会社")

    def test_unmatched_tollgate_operator_filter_includes_unresolved_records(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        db = Mock()
        db.cursor.return_value = cursor
        with (
            patch.object(etc_repository, "ensure_schema"),
            patch.object(etc_repository, "get_db", return_value=db),
        ):
            etc_repository.list_records(
                status="registered",
                operator_name="__unmatched__",
            )

        query, params = cursor.execute.call_args.args
        self.assertIn("status=%s", query)
        self.assertIn("tollgate_match_status", query)
        self.assertNotIn("__unmatched__", params)

    def test_all_records_filter_includes_source_deleted_records(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        db = Mock()
        db.cursor.return_value = cursor
        with (
            patch.object(etc_repository, "ensure_schema"),
            patch.object(etc_repository, "get_db", return_value=db),
        ):
            etc_repository.list_records(status="", limit=None)

        query, params = cursor.execute.call_args.args
        self.assertNotIn("source_state", query)
        self.assertEqual(params, ())

    def test_deleted_records_filter_only_selects_source_deleted_records(self):
        cursor = Mock()
        cursor.fetchall.return_value = []
        db = Mock()
        db.cursor.return_value = cursor
        with (
            patch.object(etc_repository, "ensure_schema"),
            patch.object(etc_repository, "get_db", return_value=db),
        ):
            etc_repository.list_records(status="deleted", limit=None)

        query, params = cursor.execute.call_args.args
        self.assertIn("source_state='deleted'", query)
        self.assertNotIn("status=%s", query)
        self.assertEqual(params, ())

    def test_exit_tollgate_matching_uses_unique_operator_and_road_only(self):
        rows = [
            {
                "operator_name": "東日本高速道路株式会社",
                "road_name": "館山自動車道",
                "tollgate_name": "市原",
            },
            {
                "operator_name": "東日本高速道路株式会社",
                "road_name": "館山自動車道",
                "tollgate_name": "市原",
            },
            {
                "operator_name": "会社A",
                "road_name": "道路A",
                "tollgate_name": "同名",
            },
            {
                "operator_name": "会社B",
                "road_name": "道路B",
                "tollgate_name": "同名",
            },
        ]
        lookup = _reference_lookup(rows)

        self.assertEqual(normalize_tollgate_name(" 市　原 "), "市原")
        self.assertEqual(
            resolve_exit_tollgate("市原", lookup),
            {
                "status": "matched",
                "operator_name": "東日本高速道路株式会社",
                "road_name": "館山自動車道",
                "matched_name": "市原",
            },
        )
        self.assertEqual(resolve_exit_tollgate("同名", lookup)["status"], "ambiguous")
        self.assertEqual(resolve_exit_tollgate("存在しない", lookup)["status"], "unmatched")

    def test_empty_status_means_all_while_missing_status_keeps_pending_default(self):
        self.assertEqual(_normalize_status_filter(None), "pending")
        self.assertEqual(_normalize_status_filter(""), "")
        self.assertEqual(_normalize_status_filter("registered"), "registered")
        self.assertEqual(_normalize_status_filter("invalid"), "pending")

    def test_invoice_issuer_catalog_contains_all_22_official_names(self):
        self.assertEqual(len(INVOICE_ISSUERS), 22)
        self.assertEqual(INVOICE_ISSUERS[0], ("T9010001095716", "東日本高速道路株式会社"))
        self.assertEqual(INVOICE_ISSUERS[-1], ("T4290005003008", "福岡北九州高速道路公社"))
        self.assertEqual(
            canonical_issuer_name("T9010001095716", "NEXCO東日本お客さまセンター"),
            "東日本高速道路株式会社",
        )

    def test_batch_records_are_registered_oldest_first(self):
        records = [
            {"id": 30, "used_at": datetime(2026, 7, 20, 21, 27)},
            {"id": 12, "used_at": datetime(2026, 6, 20, 7, 24)},
            {"id": 11, "used_at": datetime(2026, 6, 20, 7, 24)},
            {"id": 20, "used_at": datetime(2026, 7, 16, 14, 39)},
        ]

        ordered = _sort_batch_records(records)

        self.assertEqual([record["id"] for record in ordered], [11, 12, 20, 30])

    def test_month_dropdown_spans_current_month_through_same_month_fourteen_years_ago(self):
        options = _month_options(datetime(2026, 7, 21, 12, 0))
        self.assertEqual(len(options), 169)
        self.assertEqual(options[0], {"value": "202607", "label": "2026年07月"})
        self.assertEqual(options[-1], {"value": "201207", "label": "2012年07月"})
        self.assertEqual(options[7], {"value": "202512", "label": "2025年12月"})

    def test_scheduled_cli_records_automation_completion(self):
        with (
            patch("sys.argv", ["fetch_cli"]),
            patch.object(fetch_cli, "scheduled_months", return_value=["202607", "202606"]),
            patch.object(fetch_cli, "fetch_month", return_value={"status": "success"}) as fetch,
            patch.object(fetch_cli, "dispatch_pending_new_record_notifications", return_value={"status": "empty", "count": 0}),
            patch.object(fetch_cli, "record_scheduled_fetch_completed") as completed,
        ):
            exit_code = fetch_cli.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(fetch.call_count, 2)
        completed.assert_called_once_with("success")

    def test_scheduled_cli_treats_official_maintenance_as_non_error(self):
        with (
            patch("sys.argv", ["fetch_cli"]),
            patch.object(fetch_cli, "scheduled_months", return_value=["202607", "202606"]),
            patch.object(fetch_cli, "fetch_month", return_value={"status": "maintenance"}),
            patch.object(fetch_cli, "dispatch_pending_new_record_notifications", return_value={"status": "empty", "count": 0}),
            patch.object(fetch_cli, "record_scheduled_fetch_completed") as completed,
        ):
            exit_code = fetch_cli.main()

        self.assertEqual(exit_code, 0)
        completed.assert_called_once_with("maintenance")

    def test_manual_cli_does_not_change_automation_completion_time(self):
        with (
            patch("sys.argv", ["fetch_cli", "--month", "202507"]),
            patch.object(fetch_cli, "fetch_month", return_value={"status": "success"}),
            patch.object(
                fetch_cli,
                "dispatch_pending_new_record_notifications",
                return_value={"status": "sent", "count": 2},
            ) as dispatch,
            patch.object(fetch_cli, "record_scheduled_fetch_completed") as completed,
        ):
            exit_code = fetch_cli.main()

        self.assertEqual(exit_code, 0)
        dispatch.assert_called_once_with()
        completed.assert_not_called()

    def test_new_etc_records_are_rendered_as_summary_and_record_cards(self):
        batches = _discord_batches([
            {
                "record_id": 101,
                "used_at": datetime(2026, 7, 20, 13, 57),
                "entry_ic": "蘇我南",
                "exit_ic": "市原",
                "amount": 440,
                "remarks": "確認中",
            },
            {
                "record_id": 102,
                "used_at": datetime(2026, 7, 20, 21, 27),
                "entry_ic": "木更津金田第一",
                "exit_ic": "市原",
                "amount": 860,
                "remarks": "確定",
            },
        ])

        self.assertEqual(len(batches), 1)
        records, payload = batches[0]
        self.assertEqual(len(records), 2)
        self.assertEqual(len(payload["embeds"]), 3)
        self.assertIn("2件", payload["embeds"][0]["description"])
        self.assertIn("¥1,300", payload["embeds"][0]["description"])
        self.assertEqual(payload["embeds"][0]["color"], 0xFFFFFF)
        self.assertEqual(payload["embeds"][1]["title"], "🚗 蘇我南 → 市原")
        self.assertEqual(payload["embeds"][1]["color"], 0xF59E0B)
        self.assertEqual(payload["embeds"][1]["fields"][1]["value"], "**¥440**")
        self.assertEqual(payload["embeds"][1]["fields"][2]["value"], "🟠 料金確認中")
        self.assertEqual(payload["embeds"][2]["title"], "🚗 木更津金田第一 → 市原")
        self.assertEqual(payload["embeds"][2]["color"], 0x3498DB)
        self.assertEqual(payload["embeds"][2]["fields"][2]["value"], "🔵 料金確定")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_finalized_and_deleted_discord_cards_use_requested_colors(self):
        record = {
            "record_id": 301,
            "used_at": datetime(2026, 7, 20, 13, 57),
            "entry_ic": "蘇我南",
            "exit_ic": "市原",
            "amount": 440,
            "remarks": "確定",
        }

        _, finalized_payload = _discord_batches(
            [{**record, "notification_kind": "finalized"}],
            "finalized",
        )[0]
        _, deleted_payload = _discord_batches(
            [{**record, "notification_kind": "source_deleted"}],
            "source_deleted",
        )[0]

        self.assertEqual(finalized_payload["embeds"][0]["color"], 0x3498DB)
        self.assertEqual(finalized_payload["embeds"][1]["color"], 0x3498DB)
        self.assertIn("料金が確定", finalized_payload["embeds"][0]["title"])
        self.assertEqual(deleted_payload["embeds"][0]["color"], 0xEF4444)
        self.assertEqual(deleted_payload["embeds"][1]["color"], 0xEF4444)
        self.assertIn("削除", deleted_payload["embeds"][0]["title"])

    def test_manual_fetch_job_state_is_written_atomically(self):
        with TemporaryDirectory() as temporary:
            with patch.object(manual_jobs, "MANUAL_JOB_ROOT", Path(temporary)):
                job_id = manual_jobs.create_manual_fetch_job("202607")
                pending = manual_jobs.read_manual_fetch_job(job_id)
                completed = manual_jobs.update_manual_fetch_job(
                    job_id,
                    status="success",
                    result={"change_count": 2},
                )

        self.assertEqual(pending["status"], "pending")
        self.assertEqual(completed["status"], "success")
        self.assertEqual(completed["result"]["change_count"], 2)

    def test_manual_fetch_page_polls_and_reloads_only_after_changes(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "etc_accounting" / "templates" / "etc_accounting" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="etcRefreshForm"', source)
        self.assertIn('id="etcRefreshProgress"', source)
        self.assertIn("result.change_count", source)
        self.assertIn("window.location.reload()", source)

    def test_discord_record_card_includes_route_times_and_travel_duration(self):
        record = {
            "record_id": 103,
            "used_at": datetime(2026, 7, 25, 16, 56),
            "entry_at": datetime(2026, 7, 25, 16, 56),
            "exit_at": datetime(2026, 7, 25, 17, 27),
            "entry_ic": "湾岸市川",
            "exit_ic": "市原",
            "amount": 1080,
            "remarks": "確認中",
        }

        _, payload = _discord_batches([record])[0]
        fields = payload["embeds"][1]["fields"]

        self.assertEqual(fields[0]["name"], "入出日時")
        self.assertEqual(
            fields[0]["value"],
            "入口 2026/07/25 16:56\n出口 2026/07/25 17:27",
        )
        self.assertEqual(fields[1], {
            "name": "走行時間",
            "value": "0:31",
            "inline": True,
        })
        self.assertEqual(fields[2]["name"], "料金")
        self.assertEqual(fields[3]["name"], "状態")

    def test_discord_record_card_formats_over_day_and_omits_invalid_duration(self):
        over_day = {
            "record_id": 104,
            "used_at": datetime(2026, 7, 25, 16, 56),
            "entry_at": datetime(2026, 7, 25, 16, 56),
            "exit_at": datetime(2026, 7, 26, 17, 1),
            "entry_ic": "湾岸市川",
            "exit_ic": "市原",
            "amount": 1080,
            "remarks": "確定",
        }
        incomplete = {
            **over_day,
            "record_id": 105,
            "entry_at": None,
        }

        _, over_day_payload = _discord_batches([over_day])[0]
        _, incomplete_payload = _discord_batches([incomplete])[0]
        over_day_fields = over_day_payload["embeds"][1]["fields"]
        incomplete_fields = incomplete_payload["embeds"][1]["fields"]

        self.assertEqual(
            next(field["value"] for field in over_day_fields if field["name"] == "走行時間"),
            "1日 0:05",
        )
        self.assertIn("入口 未記録", incomplete_fields[0]["value"])
        self.assertNotIn("走行時間", {field["name"] for field in incomplete_fields})

    def test_discord_record_cards_are_split_at_embed_limit(self):
        records = [
            {
                "notification_id": index,
                "record_id": index,
                "used_at": datetime(2026, 7, 20, 13, index),
                "entry_ic": "市原",
                "exit_ic": "蘇我南",
                "amount": 100,
                "remarks": "確定",
            }
            for index in range(1, 12)
        ]

        batches = _discord_batches(records)

        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0][1]["embeds"]), 10)
        self.assertEqual(len(batches[1][1]["embeds"]), 2)
        self.assertEqual(batches[1][1]["content"], "🚗 ETC新規明細（続き 2/2）")

    def test_discord_record_cards_are_ordered_oldest_first(self):
        records = [
            {
                "record_id": 202,
                "used_at": datetime(2026, 7, 23, 10, 57),
                "entry_ic": "袖ヶ浦第二",
                "exit_ic": "市原",
                "amount": 700,
                "remarks": "確認中",
            },
            {
                "record_id": 201,
                "used_at": datetime(2026, 7, 23, 9, 45),
                "entry_ic": "市原",
                "exit_ic": "姉崎袖ヶ浦",
                "amount": 380,
                "remarks": "確認中",
            },
        ]

        selected, payload = _discord_batches(records)[0]

        self.assertEqual([record["record_id"] for record in selected], [201, 202])
        self.assertEqual(payload["embeds"][1]["title"], "🚗 市原 → 姉崎袖ヶ浦")
        self.assertEqual(payload["embeds"][2]["title"], "🚗 袖ヶ浦第二 → 市原")

    def test_discord_test_notification_uses_latest_two_records_in_oldest_first_order(self):
        records = [
            {
                "record_id": 202,
                "used_at": datetime(2026, 7, 23, 10, 57),
                "entry_ic": "袖ヶ浦第二",
                "exit_ic": "市原",
                "amount": 700,
                "remarks": "確認中",
            },
            {
                "record_id": 201,
                "used_at": datetime(2026, 7, 23, 9, 45),
                "entry_ic": "市原",
                "exit_ic": "姉崎袖ヶ浦",
                "amount": 380,
                "remarks": "確認中",
            },
        ]
        with (
            patch("app.etc_accounting.notifications.list_records", return_value=records) as latest,
            patch("app.etc_accounting.notifications.get_admin_discord_webhook", return_value="https://discord.example/webhook"),
            patch("app.etc_accounting.notifications._post_discord") as post,
        ):
            sent_count = send_test_notification()

        self.assertEqual(sent_count, 2)
        latest.assert_called_once_with(limit=2)
        payload = post.call_args.args[1]
        self.assertEqual(payload["content"], "✅ ETC定期取得のテスト通知です")
        self.assertEqual(payload["embeds"][1]["title"], "🚗 市原 → 姉崎袖ヶ浦")
        self.assertEqual(payload["embeds"][2]["title"], "🚗 袖ヶ浦第二 → 市原")
        self.assertIn("¥1,080", payload["embeds"][0]["description"])

    def test_discord_batch_retry_does_not_resend_completed_cards(self):
        records = [
            {
                "notification_id": index,
                "record_id": index,
                "used_at": datetime(2026, 7, 20, 13, index),
                "entry_ic": "市原",
                "exit_ic": "蘇我南",
                "amount": 100,
                "remarks": "確定",
            }
            for index in range(1, 12)
        ]
        with (
            patch("app.etc_accounting.notifications.claim_pending_record_notifications", return_value=records),
            patch("app.etc_accounting.notifications.get_admin_discord_webhook", return_value="https://discord.example/webhook"),
            patch("app.etc_accounting.notifications._post_discord", side_effect=[None, RuntimeError("2通目の送信失敗")]),
            patch("app.etc_accounting.notifications.finish_record_notifications") as finish,
        ):
            with self.assertRaisesRegex(RuntimeError, "2通目の送信失敗"):
                dispatch_pending_new_record_notifications()

        self.assertEqual(finish.call_count, 2)
        self.assertEqual(finish.call_args_list[0].args[0], list(range(1, 10)))
        self.assertEqual(finish.call_args_list[0].kwargs, {})
        self.assertEqual(finish.call_args_list[1].args[0], [10, 11])
        self.assertEqual(finish.call_args_list[1].kwargs, {"error": "2通目の送信失敗"})

    def test_pending_notifications_are_marked_sent_only_after_discord_success(self):
        records = [{
            "notification_id": 11,
            "used_at": datetime(2026, 7, 20, 13, 57),
            "entry_ic": "蘇我南",
            "exit_ic": "市原",
            "amount": 440,
            "remarks": "確定",
        }]
        with (
            patch("app.etc_accounting.notifications.claim_pending_record_notifications", return_value=records),
            patch("app.etc_accounting.notifications.get_admin_discord_webhook", return_value="https://discord.example/webhook"),
            patch("app.etc_accounting.notifications._post_discord") as post,
            patch("app.etc_accounting.notifications.finish_record_notifications") as finish,
        ):
            result = dispatch_pending_new_record_notifications()

        self.assertEqual(result, {"status": "sent", "count": 1})
        post.assert_called_once()
        finish.assert_called_once_with([11])

    def test_failed_discord_notification_remains_retryable(self):
        records = [{
            "notification_id": 12,
            "used_at": datetime(2026, 7, 20, 13, 57),
            "entry_ic": "蘇我南",
            "exit_ic": "市原",
            "amount": 440,
            "remarks": "確定",
        }]
        with (
            patch("app.etc_accounting.notifications.claim_pending_record_notifications", return_value=records),
            patch("app.etc_accounting.notifications.get_admin_discord_webhook", return_value="https://discord.example/webhook"),
            patch("app.etc_accounting.notifications._post_discord", side_effect=RuntimeError("送信失敗")),
            patch("app.etc_accounting.notifications.finish_record_notifications") as finish,
        ):
            with self.assertRaisesRegex(RuntimeError, "送信失敗"):
                dispatch_pending_new_record_notifications()

        finish.assert_called_once_with([12], error="送信失敗")

    def test_batch_eligibility_requires_final_pdf_mapping_and_unregistered_state(self):
        base = {
            "status": "pending",
            "remarks": "確定",
            "pdf_path": "/tmp/certificate.pdf",
            "invoice_registration_number": "T9010001095716",
            "freee_deal_id": None,
        }
        mapping = {"partner_id": 10, "item_id": 20}
        self.assertEqual(
            registration_eligibility(base, company_id=1, mapping=mapping, check_pdf_file=False),
            (True, "登録可能"),
        )
        provisional = {**base, "remarks": "確認中"}
        self.assertEqual(
            registration_eligibility(provisional, company_id=1, mapping=mapping, check_pdf_file=False),
            (False, "料金確認中"),
        )
        self.assertEqual(
            registration_eligibility(base, company_id=1, mapping=None, check_pdf_file=False),
            (False, "取引先・品目未設定"),
        )
        registered = {**base, "status": "registered", "freee_deal_id": 99}
        self.assertEqual(
            registration_eligibility(registered, company_id=1, mapping=mapping, check_pdf_file=False),
            (False, "freee登録済み"),
        )

    def test_batch_continues_after_one_record_fails(self):
        items = [
            {"id": 101, "record_id": 1},
            {"id": 102, "record_id": 2},
        ]
        records = {
            record_id: {
                "id": record_id,
                "status": "pending",
                "remarks": "確定",
                "pdf_path": "/tmp/certificate.pdf",
                "invoice_registration_number": "T9010001095716",
                "freee_deal_id": None,
            }
            for record_id in (1, 2)
        }
        updates = []
        with (
            patch("app.etc_accounting.batch.claim_batch_job", return_value=True),
            patch("app.etc_accounting.batch.get_batch_items", return_value=items),
            patch("app.etc_accounting.batch.get_record", side_effect=lambda record_id: records[record_id]),
            patch("app.etc_accounting.batch.get_registration_mapping", return_value={"partner_id": 10, "item_id": 20}),
            patch("app.etc_accounting.batch.registration_eligibility", return_value=(True, "登録可能")),
            patch("app.etc_accounting.batch.register_record", side_effect=[{"deal_id": 501}, RuntimeError("API失敗")]),
            patch("app.etc_accounting.batch.update_batch_item", side_effect=lambda item_id, **values: updates.append((item_id, values))),
            patch("app.etc_accounting.batch.finish_batch_job", return_value={"status": "partial"}) as finish,
            patch("app.etc_accounting.batch.freee_services.get_freee_deal_settings", return_value={"company_id": 1}),
            patch("app.etc_accounting.batch.freee_services.sanitize_freee_error", side_effect=str),
        ):
            result = run_batch_job("job-1")

        self.assertEqual(result, {"status": "partial"})
        self.assertIn((101, {"status": "success", "deal_id": 501}), updates)
        self.assertIn((102, {"status": "failed", "error": "API失敗"}), updates)
        finish.assert_called_once_with("job-1")

    def test_freee_master_requests_all_partners_and_items(self):
        calls = []

        def fake_request(method, path, **kwargs):
            calls.append((path, kwargs.get("params") or {}))
            if path == "/api/1/companies":
                return {"companies": [{"id": 1}]}
            if path == "/api/1/taxes/companies/1":
                return {"taxes": []}
            if path == "/api/1/taxes/codes":
                return {"taxes": []}
            key = {
                "/api/1/account_items": "account_items",
                "/api/1/items": "items",
                "/api/1/partners": "partners",
                "/api/1/walletables": "walletables",
            }.get(path)
            return {key: []} if key else {}

        with (
            patch.object(freee_services, "freee_api_request", side_effect=fake_request),
            patch.object(freee_services, "get_freee_common_settings", return_value={"company_id": 1}),
        ):
            freee_services.fetch_freee_master_bundle(1)

        params_by_path = {path: params for path, params in calls}
        self.assertEqual(params_by_path["/api/1/partners"]["limit"], 3000)
        self.assertEqual(params_by_path["/api/1/items"]["limit"], 3000)
        self.assertEqual(params_by_path["/api/1/account_items"]["limit"], 3000)
        self.assertNotIn("limit", params_by_path["/api/1/walletables"])

    def test_settings_template_renders_item_master_list(self):
        from app import app

        master = {
            "warnings": [],
            "account_items": [],
            "taxes": [],
            "walletables": [],
            "partners": [{"id": 10, "name": "NEXCO東日本"}],
            "items": [{"id": 20, "name": "ETC利用料"}],
        }
        with app.test_request_context("/etc-accounting/settings"):
            rendered = render_template(
                "etc_accounting/settings.html",
                master=master,
                current={},
                registration_mappings=[{
                    "registration_number": "T9010001095716",
                    "issuer_name": "NEXCO東日本お客さまセンター",
                    "record_count": 1,
                    "unregistered_count": 1,
                    "configured": False,
                }],
                etc_credentials={},
                csrf_token="test-token",
                tax_code=lambda row: row.get("code"),
                walletable_id=lambda row: row.get("id"),
                walletable_type=lambda row: row.get("type"),
                format_account=lambda row: row.get("name", ""),
                format_tax=lambda row: row.get("name", ""),
                format_wallet=lambda row: row.get("name", ""),
                format_partner=lambda row: row.get("name", ""),
                format_item=lambda row: row.get("name", ""),
            )
        self.assertIn("ETC利用料", rendered)
        self.assertIn("T9010001095716", rendered)

    def test_credentials_are_encrypted_and_login_failure_resets_on_save(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch.object(etc_credentials, "CREDENTIALS_ROOT", root),
                patch.object(etc_credentials, "CREDENTIALS_FILE", root / "credentials.enc"),
                patch.object(etc_credentials, "LOCK_FILE", root / "browser.lock"),
                patch.object(etc_credentials, "FAILURE_FILE", root / "login_failure.json"),
                patch.dict(os.environ, {"ETC_CREDENTIALS_KEY": "test-encryption-key"}),
            ):
                etc_credentials.save_credentials("TestUser", "SecretPassword123!")
                encrypted = (root / "credentials.enc").read_bytes()
                self.assertNotIn(b"TestUser", encrypted)
                self.assertNotIn(b"SecretPassword123!", encrypted)
                self.assertEqual(
                    etc_credentials.load_credentials(),
                    {"login_id": "TestUser", "password": "SecretPassword123!"},
                )
                etc_credentials.record_login_failure()
                self.assertTrue(etc_credentials.credentials_status()["login_blocked"])
                etc_credentials.save_credentials("TestUser", "UpdatedPassword123!")
                self.assertFalse(etc_credentials.credentials_status()["login_blocked"])

    def test_browser_login_submits_site_form(self):
        browser = object.__new__(ETCTargetPage)
        browser.navigate = Mock()
        browser.is_logged_in = Mock(side_effect=[False, True])
        browser.evaluate = Mock(side_effect=["https://www2.etc-meisai.jp/etc/R", None, True])
        browser.wait_navigation = Mock()

        browser.login_with_credentials("TestUser", "SecretPassword123!")

        browser.navigate.assert_called_once()
        login_expression = next(
            call.args[0] for call in browser.evaluate.call_args_list
            if "risLoginId" in call.args[0]
        )
        self.assertIn("risPassword", login_expression)
        browser.wait_navigation.assert_called_once()

    def test_official_maintenance_page_is_detected(self):
        response = Mock(
            ok=True,
            text="<h1>メンテナンスに伴うサービス一時停止のお知らせ</h1>"
                 "<p>メンテナンス作業のため、7月29日（水）8時頃まで利用の停止をさせていただいております。</p>",
        )
        with (
            patch.object(etc_browser_session.requests, "get", return_value=response),
            patch.object(
                etc_browser_session,
                "_maintenance_cache",
                {"checked_at": 0.0, "active": False, "message": ""},
            ),
        ):
            result = etc_browser_session.etc_maintenance_status(force=True)

        self.assertTrue(result["active"])
        self.assertIn("ETC側メンテナンス中", result["message"])
        self.assertIn("7月29日", result["message"])

    def test_fetch_month_returns_maintenance_without_error(self):
        browser = MagicMock()
        browser.__enter__.return_value = browser
        browser.__exit__.return_value = False
        browser.open_statement_month.side_effect = ETCMaintenanceError("ETC側メンテナンス中です。")
        lock = Mock()
        with (
            patch("app.etc_accounting.fetcher.acquire_fetch_lock", return_value=lock),
            patch("app.etc_accounting.fetcher.release_fetch_lock") as release_lock,
            patch("app.etc_accounting.fetcher.start_run", return_value=99),
            patch("app.etc_accounting.fetcher.finish_run") as finish_run,
            patch("app.etc_accounting.fetcher.etc_browser_lock", return_value=MagicMock()),
            patch("app.etc_accounting.fetcher.ETCTargetPage", return_value=browser),
        ):
            result = fetch_month("202607")

        self.assertEqual(result["status"], "maintenance")
        finish_run.assert_called_once_with(
            99,
            status="maintenance",
            found=0,
            downloaded=0,
            skipped=0,
            error="ETC側メンテナンス中です。",
        )
        release_lock.assert_called_once_with(lock)

    def test_parse_statement_page_extracts_one_transaction(self):
        page = parse_statement_page(STATEMENT_HTML, "202606")
        self.assertEqual(page.page_numbers, [1, 2])
        self.assertEqual(page.form_token, "token-value")
        self.assertEqual(
            page.records,
            [{
                "transaction_key": "202606010625-example",
                "statement_month": "202606",
                "used_at": datetime(2026, 6, 1, 6, 25),
                "entry_at": None,
                "exit_at": datetime(2026, 6, 1, 6, 25),
                "entry_ic": "市原",
                "exit_ic": "千葉西",
                "amount": 750,
                "vehicle_type": "5",
                "vehicle_number": "",
                "card_mask": "********2159",
                "redemption_amount": 0,
                "postpaid_amount": 750,
                "remarks": "確定",
            }],
        )

    def test_parse_statement_page_extracts_vehicle_number_and_payment_breakdown(self):
        html = """
        <html><body><form name="frm"><input name="p" value="token"><table><tr>
          <td><input type="checkbox" name="hakkoMeisai" value="vehicle-example"></td>
          <td>26/08/01 17:58 姉崎袖ヶ浦 26/08/01 18:30 市原</td>
          <td>270</td><td>110<br>160</td>
          <td><span>5<br>19<br>********2159</span></td><td>確定</td>
        </tr></table></form></body></html>
        """
        record = parse_statement_page(html, "202608").records[0]
        self.assertEqual(record["vehicle_type"], "5")
        self.assertEqual(record["vehicle_number"], "19")
        self.assertEqual(record["card_mask"], "********2159")
        self.assertEqual(record["redemption_amount"], 110)
        self.assertEqual(record["postpaid_amount"], 160)

    def test_parse_statement_page_preserves_blank_vehicle_number(self):
        html = """
        <html><body><form name="frm"><input name="p" value="token"><table><tr>
          <td><input type="checkbox" name="hakkoMeisai" value="no-vehicle-example"></td>
          <td>企画割引 26/08/02 20:09 新空港</td>
          <td>2,500</td><td>0<br>2,500</td>
          <td><span>5<br><br>********2159</span></td><td>確定</td>
        </tr></table></form></body></html>
        """
        record = parse_statement_page(html, "202608").records[0]
        self.assertEqual(record["vehicle_number"], "")
        self.assertEqual(record["postpaid_amount"], 2500)

    def test_parse_statement_requires_login(self):
        with self.assertRaises(ETCAuthenticationRequired):
            parse_statement_page("<html><title>ログイン</title></html>", "202606")

    def test_parse_provisional_route_with_entry_and_exit_timestamps(self):
        html = """
        <html><body><form name="frm"><input name="p" value="token"><table><tr>
          <td><input type="checkbox" name="hakkoMeisai" value="provisional-example"></td>
          <td>26/07/20 13:57 蘇我南 26/07/20 14:04 市原</td>
          <td>440</td><td></td><td>5 ********2159</td><td>確認中</td>
        </tr></table></form></body></html>
        """
        record = parse_statement_page(html, "202607").records[0]
        self.assertEqual(record["used_at"], datetime(2026, 7, 20, 13, 57))
        self.assertEqual(record["entry_at"], datetime(2026, 7, 20, 13, 57))
        self.assertEqual(record["exit_at"], datetime(2026, 7, 20, 14, 4))
        self.assertEqual(record["entry_ic"], "蘇我南")
        self.assertEqual(record["exit_ic"], "市原")
        self.assertTrue(is_provisional_record(record))

    def test_provisional_record_is_rejected_before_freee_registration(self):
        record = {"id": 30, "remarks": "確認中", "freee_deal_id": None}
        with (
            patch("app.etc_accounting.freee_sync.get_record", return_value=record),
            patch("app.etc_accounting.freee_sync.claim_registration") as claim,
        ):
            with self.assertRaisesRegex(RuntimeError, "料金確認中"):
                register_record(30)
        claim.assert_not_called()

    def test_scheduled_months_includes_current_and_previous(self):
        self.assertEqual(scheduled_months(datetime(2026, 7, 20), 2), ["202607", "202606"])

    def test_page_navigation_clears_certificate_selection(self):
        browser = object.__new__(ETCTargetPage)
        calls = []
        browser.evaluate = lambda expression: calls.append(("evaluate", expression))
        browser._submit = lambda path: calls.append(("submit", path))

        browser.go_to_page(3)

        self.assertEqual([kind for kind, _ in calls], ["evaluate", "submit"])
        self.assertIn('input[name="hakkoMeisai"]', calls[0][1])
        self.assertIn("input.checked = false", calls[0][1])
        self.assertIn("input.disabled = true", calls[0][1])
        self.assertIn("pageNo=3", calls[1][1])

    def test_freee_deal_payload_attaches_receipt_id(self):
        record = {
            "id": 12,
            "used_at": datetime(2026, 6, 1, 6, 25),
            "entry_ic": "市原",
            "exit_ic": "千葉西",
            "amount": 750,
        }
        settings = {
            "company_id": 1,
            "account_item_id": 2,
            "tax_code": 3,
            "deal_payment_mode": "settled",
            "walletable_type": "credit_card",
            "walletable_id": 4,
        }
        payload = _deal_payload(record, settings, 99)
        self.assertEqual(payload["receipt_ids"], [99])
        self.assertEqual(payload["details"][0]["amount"], 750)
        self.assertEqual(payload["payments"][0]["from_walletable_type"], "credit_card")

    def test_freee_description_includes_entry_and_exit_times(self):
        record = {
            "used_at": datetime(2026, 7, 27, 14, 14),
            "entry_at": datetime(2026, 7, 27, 14, 14),
            "exit_at": datetime(2026, 7, 27, 14, 42),
            "entry_ic": "湾岸市川",
            "exit_ic": "市原",
        }
        self.assertEqual(
            _description(record),
            "ETC通行料金 湾岸市川 7/27 14:14 → 市原 7/27 14:42",
        )

    def test_freee_deal_payload_uses_registration_mapping(self):
        record = {
            "id": 12,
            "used_at": datetime(2026, 6, 1, 6, 25),
            "entry_ic": "市原",
            "exit_ic": "千葉西",
            "amount": 750,
        }
        settings = {
            "company_id": 1,
            "account_item_id": 2,
            "tax_code": 3,
            "deal_payment_mode": "unsettled",
            "partner_id": 999,
        }
        payload = _deal_payload(
            record,
            settings,
            99,
            {"partner_id": 123, "item_id": 456},
        )
        self.assertEqual(payload["partner_id"], 123)
        self.assertEqual(payload["details"][0]["item_id"], 456)

    def test_freee_deal_update_payload_preserves_detail_and_omits_payments(self):
        record = {
            "id": 12,
            "used_at": datetime(2026, 6, 1, 6, 25),
            "entry_ic": "市原",
            "exit_ic": "千葉西",
            "amount": 750,
        }
        settings = {
            "company_id": 1,
            "account_item_id": 2,
            "tax_code": 3,
            "deal_payment_mode": "settled",
            "walletable_type": "credit_card",
            "walletable_id": 4,
        }
        payload = _deal_update_payload(
            record,
            settings,
            99,
            {"partner_id": 123, "item_id": 456},
            789,
            [98, 99],
        )
        self.assertNotIn("payments", payload)
        self.assertEqual(payload["details"][0]["id"], 789)
        self.assertEqual(payload["details"][0]["item_id"], 456)
        self.assertEqual(payload["partner_id"], 123)
        self.assertEqual(payload["receipt_ids"], [98, 99])

    def test_registered_deal_is_updated_without_creating_duplicate(self):
        record = {
            "id": 28,
            "status": "registered",
            "remarks": "確定",
            "used_at": datetime(2026, 6, 20, 9, 12),
            "entry_ic": "千葉西",
            "exit_ic": "市原",
            "amount": 750,
            "freee_deal_id": 100,
            "freee_receipt_id": 200,
            "invoice_registration_number": "T9010001095716",
        }
        settings = {
            "company_id": 1,
            "account_item_id": 2,
            "tax_code": 3,
            "deal_payment_mode": "unsettled",
        }
        mapping = {"partner_id": 10, "item_id": 20}
        responses = [
            {"deal": {"id": 100, "type": "expense", "details": [{"id": 300}], "receipt_ids": [200]}},
            {"deal": {"id": 100}},
        ]
        with (
            patch("app.etc_accounting.freee_sync.get_record", return_value=record),
            patch("app.etc_accounting.freee_sync._settings", return_value=settings),
            patch("app.etc_accounting.freee_sync.get_registration_mapping", return_value=mapping),
            patch("app.etc_accounting.freee_sync.claim_registered_update", return_value=True),
            patch("app.etc_accounting.freee_sync._ensure_receipt_for_update", return_value=200) as ensure_receipt,
            patch("app.etc_accounting.freee_sync.update_registration") as update_registration,
            patch("app.etc_accounting.freee_sync.freee_services.freee_api_request", side_effect=responses) as request,
        ):
            result = update_registered_record(28)

        self.assertEqual(result, {"status": "updated", "deal_id": 100, "receipt_id": 200})
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "PUT"])
        self.assertFalse(ensure_receipt.call_args.kwargs["force_new"])
        self.assertNotIn("payments", request.call_args_list[1].kwargs["json_body"])
        update_registration.assert_called_with(28, status="registered", freee_error=None)

    def test_deleted_registered_deal_is_reregistered_only_after_404(self):
        record = {
            "id": 28,
            "status": "registered",
            "remarks": "確定",
            "used_at": datetime(2026, 6, 20, 9, 12),
            "entry_ic": "千葉西",
            "exit_ic": "市原",
            "amount": 750,
            "freee_deal_id": 100,
            "freee_receipt_id": 200,
            "invoice_registration_number": "T9010001095716",
        }
        settings = {
            "company_id": 1,
            "account_item_id": 2,
            "tax_code": 3,
            "deal_payment_mode": "unsettled",
        }
        mapping = {"partner_id": 10, "item_id": 20}
        with (
            patch("app.etc_accounting.freee_sync.get_record", return_value=record),
            patch("app.etc_accounting.freee_sync._settings", return_value=settings),
            patch("app.etc_accounting.freee_sync.get_registration_mapping", return_value=mapping),
            patch("app.etc_accounting.freee_sync.claim_registered_update", return_value=True),
            patch("app.etc_accounting.freee_sync._ensure_receipt_for_update", return_value=200) as ensure_receipt,
            patch("app.etc_accounting.freee_sync.update_registration") as update_registration,
            patch(
                "app.etc_accounting.freee_sync.freee_services.freee_api_request",
                side_effect=[RuntimeError("freee API error: HTTP 404"), {"deal": {"id": 101}}],
            ) as request,
        ):
            result = update_registered_record(28)

        self.assertEqual(result["status"], "reregistered")
        self.assertEqual(result["deal_id"], 101)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["GET", "POST"])
        self.assertTrue(ensure_receipt.call_args.kwargs["force_new"])
        self.assertEqual(update_registration.call_args.kwargs["freee_deal_id"], 101)

    def test_deleted_receipt_returning_http_400_is_reuploaded(self):
        record = {
            "id": 28,
            "used_at": datetime(2026, 6, 20, 9, 12),
            "entry_ic": "市原",
            "exit_ic": "千葉西",
            "amount": 750,
            "freee_receipt_id": 200,
            "pdf_path": "/tmp/certificate.pdf",
        }
        with (
            patch(
                "app.etc_accounting.freee_sync._update_receipt_invoice_metadata",
                side_effect=[
                    RuntimeError('freee API error: HTTP 400 {"messages":["証憑は既に削除されています。"]}'),
                    "T9010001095716",
                ],
            ) as update_metadata,
            patch("app.etc_accounting.freee_sync._upload_pdf", return_value=201) as upload,
            patch("app.etc_accounting.freee_sync.update_registration") as update_registration,
        ):
            receipt_id = _ensure_receipt_for_update(record, 1, "T9010001095716")

        self.assertEqual(receipt_id, 201)
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args, (record, 1))
        self.assertIn("mfu-reregister-28-", upload.call_args.kwargs["upload_name"])
        self.assertTrue(upload.call_args.kwargs["unique_content"])
        update_registration.assert_called_once_with(28, freee_receipt_id=201)
        self.assertEqual(update_metadata.call_count, 2)

    def test_deleted_deal_forces_new_receipt_without_touching_old_receipt(self):
        record = {
            "id": 28,
            "used_at": datetime(2026, 6, 20, 9, 12),
            "entry_ic": "市原",
            "exit_ic": "千葉西",
            "amount": 750,
            "freee_receipt_id": 200,
            "pdf_path": "/tmp/certificate.pdf",
        }
        with (
            patch("app.etc_accounting.freee_sync._update_receipt_invoice_metadata") as update_metadata,
            patch("app.etc_accounting.freee_sync._upload_pdf", return_value=201),
            patch("app.etc_accounting.freee_sync.update_registration"),
        ):
            receipt_id = _ensure_receipt_for_update(
                record,
                1,
                "T9010001095716",
                force_new=True,
            )

        self.assertEqual(receipt_id, 201)
        update_metadata.assert_called_once_with(record, 1, 201, "T9010001095716")

    def test_unique_receipt_upload_changes_only_uploaded_copy(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "certificate.pdf"
            original = b"%PDF-1.4\nbody\nstartxref\n0\n%%EOF\n"
            pdf_path.write_bytes(original)
            record = {
                "id": 28,
                "used_at": datetime(2026, 6, 20, 9, 12),
                "entry_ic": "市原",
                "exit_ic": "千葉西",
                "amount": 750,
                "pdf_path": str(pdf_path),
            }
            uploaded = {}

            def fake_upload(*args, **kwargs):
                uploaded["bytes"] = kwargs["files"]["receipt"][1].read()
                return {"receipt": {"id": 201}}

            with patch(
                "app.etc_accounting.freee_sync.freee_services.freee_api_multipart_request",
                side_effect=fake_upload,
            ):
                receipt_id = _upload_pdf(record, 1, unique_content=True)
            persisted = pdf_path.read_bytes()

        self.assertEqual(receipt_id, 201)
        self.assertEqual(persisted, original)
        self.assertNotEqual(uploaded["bytes"], original)
        self.assertIn(b"MFU re-registration source-sha256=", uploaded["bytes"])
        self.assertTrue(uploaded["bytes"].startswith(b"%PDF-1.4"))

    def test_unmapped_registration_number_is_rejected_before_freee_registration(self):
        record = {
            "id": 30,
            "remarks": "確定",
            "freee_deal_id": None,
            "invoice_registration_number": "T9010001095716",
        }
        with (
            patch("app.etc_accounting.freee_sync.get_record", return_value=record),
            patch("app.etc_accounting.freee_sync._settings", return_value={"company_id": 1}),
            patch("app.etc_accounting.freee_sync.get_registration_mapping", return_value=None),
            patch("app.etc_accounting.freee_sync.claim_registration") as claim,
        ):
            with self.assertRaisesRegex(RuntimeError, "取引先・品目が未設定"):
                register_record(30)
        claim.assert_not_called()

    def test_freee_pdf_upload_uses_receipt_multipart_field(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            pdf_path = Path(directory) / "certificate.pdf"
            pdf_path.write_bytes(b"%PDF-1.4 test")
            record = {
                "used_at": datetime(2026, 6, 1, 6, 25),
                "entry_ic": "市原",
                "exit_ic": "千葉西",
                "amount": 750,
                "pdf_path": str(pdf_path),
            }
            with patch("app.etc_accounting.freee_sync.freee_services.freee_api_multipart_request") as request:
                request.return_value = {"receipt": {"id": 88}}
                self.assertEqual(_upload_pdf(record, 1), 88)
            self.assertIn("receipt", request.call_args.kwargs["files"])
            self.assertEqual(request.call_args.kwargs["data"]["qualified_invoice"], "qualified")

    def test_parse_invoice_registration_number_from_pdf_text(self):
        text = "登録番号：Ｔ９０１０００１０９５７１６"
        self.assertEqual(_parse_invoice_registration_number(text), "T9010001095716")

    def test_parse_invoice_issuer_name_near_registration_number(self):
        text = """
        利 用 証 明 書
        NEXCO東日本お客さまセンター
        0570-024-024
        登録番号：T9010001095716
        料金所(自) 千葉西
        """
        self.assertEqual(
            parse_invoice_issuer_name(text, "T9010001095716"),
            "NEXCO東日本お客さまセンター",
        )

    def test_missing_invoice_registration_number_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "記載されていません"):
            _parse_invoice_registration_number("登録番号なし")

    def test_freee_receipt_metadata_includes_pdf_registration_number(self):
        record = {
            "used_at": datetime(2026, 6, 20, 9, 12),
            "entry_ic": "浦和本線",
            "exit_ic": "館林",
            "amount": 1310,
            "pdf_path": "/tmp/certificate.pdf",
        }
        with (
            patch("app.etc_accounting.freee_sync._invoice_registration_number", return_value="T9010001095716"),
            patch("app.etc_accounting.freee_sync.freee_services.freee_api_request") as request,
        ):
            number = _update_receipt_invoice_metadata(record, 123, 456)

        self.assertEqual(number, "T9010001095716")
        request.assert_called_once_with(
            "PUT",
            "/api/1/receipts/456",
            json_body={
                "company_id": 123,
                "description": "ETC通行料金 浦和本線 → 館林",
                "receipt_metadatum": {
                    "partner_name": "ETC利用照会サービス",
                    "issue_date": "2026-06-20",
                    "amount": 1310,
                },
                "qualified_invoice": "qualified",
                "invoice_registration_number": "T9010001095716",
                "document_type": "receipt",
            },
        )
