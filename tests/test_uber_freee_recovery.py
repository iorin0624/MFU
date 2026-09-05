from datetime import date
from unittest import TestCase
from unittest.mock import patch

from app.records.bp import (
    _find_uber_freee_deals_by_ref_number,
    _recover_missing_uber_freee_deal,
    _update_uber_row_to_freee,
)


def _row():
    return {
        "id": 17,
        "work_date": date(2026, 6, 5),
        "freee_deal_id": 999,
        "deliveries": 2,
        "net_yen": 800,
        "promo_yen": 100,
        "other_yen": 0,
        "tip_yen": 0,
        "total_yen": 900,
    }


def _settings():
    return {
        "company_id": 10,
        "account_item_id": 20,
        "tax_code": 21,
        "walletable_type": "wallet",
        "walletable_id": 30,
        "deal_payment_mode": "settled",
    }


class UberFreeeRecoveryTest(TestCase):
    @patch("app.records.bp.freee_services.freee_api_request")
    def test_find_uber_freee_deal_filters_exact_reference(self, mock_request):
        mock_request.return_value = {
            "deals": [
                {"id": 1, "ref_number": "other"},
                {"id": 2, "ref_number": "uber-20260605"},
            ]
        }

        self.assertEqual(
            _find_uber_freee_deals_by_ref_number(date(2026, 6, 5), 10),
            [{"id": 2, "ref_number": "uber-20260605"}],
        )
        self.assertEqual(mock_request.call_args.kwargs["params"]["issue_date_start"], "2026-06-05")
        self.assertEqual(mock_request.call_args.kwargs["params"]["issue_date_end"], "2026-06-05")

    @patch("app.records.bp._save_uber_freee_link")
    @patch("app.records.bp._find_uber_freee_deals_by_ref_number", return_value=[])
    @patch("app.records.bp.freee_services.freee_api_request", return_value={"deal": {"id": 1234}})
    def test_recover_missing_deal_creates_and_relinks(self, mock_request, _mock_find, mock_save):
        result = _recover_missing_uber_freee_deal(_row(), _settings(), {"company_id": 10, "payments": []})

        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["recovery"], "recreated")
        mock_request.assert_called_once_with("POST", "/api/1/deals", json_body={"company_id": 10, "payments": []})
        mock_save.assert_called_once_with(17, 1234)

    @patch("app.records.bp._save_uber_freee_link")
    @patch("app.records.bp._sync_uber_freee_payment")
    @patch("app.records.bp._find_uber_freee_deals_by_ref_number", return_value=[{"id": 4321}])
    @patch("app.records.bp.freee_services.freee_api_request", return_value={})
    def test_recover_missing_deal_updates_exact_match(self, mock_request, _mock_find, mock_payment, mock_save):
        payload = {"company_id": 10, "details": [], "payments": [{"amount": 900}]}
        result = _recover_missing_uber_freee_deal(_row(), _settings(), payload)

        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["recovery"], "relinked")
        mock_request.assert_called_once_with(
            "PUT", "/api/1/deals/4321", json_body={"company_id": 10, "details": []}
        )
        mock_payment.assert_called_once_with(4321, payload)
        mock_save.assert_called_once_with(17, 4321)

    @patch("app.records.bp._find_uber_freee_deals_by_ref_number", return_value=[{"id": 1}, {"id": 2}])
    def test_recover_missing_deal_stops_on_duplicate_reference(self, _mock_find):
        with self.assertRaisesRegex(RuntimeError, "複数あります"):
            _recover_missing_uber_freee_deal(_row(), _settings(), {"company_id": 10})

    @patch("app.records.bp._mark_uber_freee_error")
    @patch("app.records.bp._recover_missing_uber_freee_deal")
    @patch("app.records.bp.freee_services.freee_api_request")
    def test_update_recovers_only_missing_deal_error(self, mock_request, mock_recover, mock_mark):
        mock_request.side_effect = RuntimeError("HTTP 400 指定された取引は存在しません。")
        mock_recover.return_value = {"date": "2026-06-05", "status": "synced", "recovery": "recreated"}

        result = _update_uber_row_to_freee(_row(), _settings())

        self.assertEqual(result["recovery"], "recreated")
        mock_recover.assert_called_once()
        mock_mark.assert_not_called()
