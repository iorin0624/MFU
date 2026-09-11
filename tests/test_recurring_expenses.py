from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from app.recurring_expenses import freee_sync
from app.recurring_expenses.repository import _is_due
from app.recurring_expenses.routes import _master_draft
from werkzeug.datastructures import MultiDict


def _master(**overrides):
    value = {
        "start_month": "2026-01",
        "end_month": None,
        "frequency_months": 1,
    }
    value.update(overrides)
    return value


def _month_item(**overrides):
    value = {
        "id": 7,
        "master_id": 3,
        "target_month": "2026-09",
        "name": "携帯電話代",
        "issue_date": date(2026, 9, 10),
        "actual_amount": 5500,
        "account_item_id": 11,
        "tax_code": 136,
        "item_id": None,
        "partner_id": None,
        "payment_mode": "settled",
        "walletable_type": "bank_account",
        "walletable_id": 22,
    }
    value.update(overrides)
    return value


def test_monthly_and_annual_due_calculation():
    assert _is_due(_master(), "2026-09")
    assert _is_due(_master(frequency_months=12), "2027-01")
    assert not _is_due(_master(frequency_months=12), "2026-09")
    assert not _is_due(_master(end_month="2026-08"), "2026-09")


def test_deal_payload_contains_expense_receipts_and_stable_reference():
    payload = freee_sync._deal_payload(_month_item(), 1, [101, 102])
    assert payload["type"] == "expense"
    assert payload["ref_number"] == "MFU-RECURRING-3-202609"
    assert payload["receipt_ids"] == [101, 102]
    assert payload["details"][0]["amount"] == 5500
    assert payload["payments"][0]["amount"] == 5500


def test_image_attachment_is_converted_to_pdf_for_freee():
    with TemporaryDirectory() as temporary:
        source = Path(temporary) / "receipt.png"
        Image.new("RGB", (32, 24), "white").save(source)
        name, stream = freee_sync._attachment_pdf({
            "file_path": str(source),
            "mime_type": "image/png",
            "original_name": "receipt.png",
        })
        try:
            assert name == "receipt.pdf"
            assert stream.read(5) == b"%PDF-"
        finally:
            stream.close()


def test_required_receipt_blocks_registration_before_claim():
    item = _month_item(receipt_required=1, freee_deal_id=None, status="pending", registration_mode="create")
    with (
        patch.object(freee_sync, "get_month_item", return_value=item),
        patch.object(freee_sync, "list_attachments", return_value=[]),
        patch.object(freee_sync, "claim_registration") as claim,
    ):
        try:
            freee_sync.register_month(7)
        except RuntimeError as exc:
            assert "添付が必須" in str(exc)
        else:
            raise AssertionError("Registration without a required receipt was accepted")
    claim.assert_not_called()


def test_registration_creates_one_deal_and_persists_ids():
    item = _month_item(receipt_required=0, freee_deal_id=None, status="pending", registration_mode="create")
    with (
        patch.object(freee_sync, "get_month_item", return_value=item),
        patch.object(freee_sync, "list_attachments", return_value=[]),
        patch.object(freee_sync, "claim_registration", return_value=True),
        patch.object(freee_sync, "_company_id", return_value=1),
        patch.object(freee_sync, "_receipt_ids", return_value=[]),
        patch.object(freee_sync, "_find_deal", return_value=None),
        patch.object(
            freee_sync.freee_services,
            "freee_api_request",
            return_value={"deal": {"id": 987}},
        ) as api_request,
        patch.object(freee_sync, "set_registration") as set_registration,
    ):
        result = freee_sync.register_month(7)

    assert result["deal_id"] == 987
    assert api_request.call_count == 1
    set_registration.assert_called_once_with(7, status="registered", deal_id=987)


def test_invalid_master_form_is_preserved_as_a_typed_draft():
    draft = _master_draft(MultiDict({
        "name": "携帯電話代",
        "default_amount": "5500",
        "frequency_months": "1",
        "account_item_id": "123",
        "notes": "入力途中のメモ",
        "receipt_required": "1",
        "return_month": "2026-09",
    }), None)
    assert draft["name"] == "携帯電話代"
    assert draft["default_amount"] == "5500"
    assert draft["account_item_id"] == 123
    assert draft["receipt_required"] == 1
    assert draft["is_active"] == 0
