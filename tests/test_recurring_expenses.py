from datetime import date
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from app.recurring_expenses import freee_sync
from app.recurring_expenses import partners
from app.recurring_expenses.repository import _is_due
from app.recurring_expenses import repository
from app.recurring_expenses.routes import _batch_candidate_ids, _master_draft
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
    assert payload["ref_number"] == "MR20260928e596f44fa6"
    assert len(payload["ref_number"]) == 20
    assert payload["ref_number"].startswith("MR202609")
    assert payload["receipt_ids"] == [101, 102]
    assert payload["details"][0]["amount"] == 5500
    assert payload["payments"][0]["amount"] == 5500


def test_deal_lookup_accepts_legacy_reference_to_prevent_duplicates():
    legacy = "MFU-RECURRING-3-202609"
    with patch.object(
        freee_sync.freee_services,
        "freee_api_request",
        return_value={"deals": [{"id": 44, "ref_number": legacy}]},
    ):
        assert freee_sync._find_deal(_month_item(), 1)["id"] == 44


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


def test_registered_month_delete_verifies_reference_and_clears_local_link():
    item = _month_item(
        freee_deal_id=987,
        status="registered",
        registration_mode="create",
    )
    deal = {"id": 987, "ref_number": freee_sync._ref_number(item)}
    with (
        patch.object(freee_sync, "get_month_item", return_value=item),
        patch.object(freee_sync, "_company_id", return_value=1),
        patch.object(freee_sync, "_deal_by_id", return_value=deal),
        patch.object(freee_sync.freee_services, "freee_api_request", return_value={}) as request,
        patch.object(freee_sync, "clear_registration") as clear,
    ):
        result = freee_sync.delete_registered_month(7)

    assert result == {"status": "deleted", "deal_id": 987}
    request.assert_called_once_with(
        "DELETE", "/api/1/deals/987", params={"company_id": 1}
    )
    clear.assert_called_once_with(7)


def test_registered_month_delete_refuses_unrelated_freee_deal():
    item = _month_item(
        freee_deal_id=987,
        status="registered",
        registration_mode="create",
    )
    with (
        patch.object(freee_sync, "get_month_item", return_value=item),
        patch.object(freee_sync, "_company_id", return_value=1),
        patch.object(freee_sync, "_deal_by_id", return_value={"id": 987, "ref_number": "OTHER"}),
        patch.object(freee_sync.freee_services, "freee_api_request") as request,
        patch.object(freee_sync, "clear_registration") as clear,
    ):
        try:
            freee_sync.delete_registered_month(7)
        except RuntimeError as exc:
            assert "管理番号が一致しない" in str(exc)
        else:
            raise AssertionError("An unrelated freee deal was deleted")

    request.assert_not_called()
    clear.assert_not_called()


def test_master_schedule_change_hides_and_prunes_old_generated_months():
    source = inspect.getsource(repository.save_master)
    list_source = inspect.getsource(repository.list_month_items)

    assert "m.target_month < %s" in source
    assert "m.freee_deal_id IS NULL" in source
    assert "a.id IS NULL" in source
    assert "_is_due(row, target_month)" in list_source


def test_freee_delete_route_removes_mfu_attachments_and_manual_delete_allows_orphans():
    route_source = inspect.getsource(
        __import__("app.recurring_expenses.routes", fromlist=["month_freee_delete"]).month_freee_delete
    )
    delete_source = inspect.getsource(repository.delete_attachment)
    template = (
        Path(__file__).resolve().parents[1]
        / "recurring_expenses/templates/recurring_expenses/index.html"
    ).read_text(encoding="utf-8")

    assert "delete_month_attachments(month_id)" in route_source
    assert 'Path(attachment["file_path"]).unlink(missing_ok=True)' in route_source
    assert 'not row.get("freee_deal_id")' in delete_source
    assert "recurring_expenses.attachment_delete" in template
    assert "MFU側の添付ファイルを削除しますか？" in template


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


def test_partner_creation_reuses_exact_existing_partner():
    with (
        patch.object(partners.freee_services, "get_freee_common_settings", return_value={"company_id": 1}),
        patch.object(partners.freee_services, "freee_api_request", return_value={"partners": [{"id": 8, "name": "株式会社テスト"}]}) as request,
    ):
        result = partners.create_or_find_partner(" 株式会社テスト ")
    assert result["status"] == "existing"
    assert result["partner"]["id"] == 8
    assert request.call_count == 1


def test_partner_creation_requires_confirmation_for_similar_name():
    with (
        patch.object(partners.freee_services, "get_freee_common_settings", return_value={"company_id": 1}),
        patch.object(partners.freee_services, "freee_api_request", return_value={"partners": [{"id": 8, "name": "株式会社テスト"}]}) as request,
    ):
        result = partners.create_or_find_partner("株式会社テストー")
    assert result["status"] == "confirmation_required"
    assert result["candidates"][0]["id"] == 8
    assert request.call_count == 1


def test_partner_creation_sends_optional_code_after_confirmation():
    responses = [
        {"partners": [{"id": 8, "name": "株式会社テスト"}]},
        {"partner": {"id": 9, "name": "株式会社テストー", "code": "T-009"}},
    ]
    with (
        patch.object(partners.freee_services, "get_freee_common_settings", return_value={"company_id": 1}),
        patch.object(partners.freee_services, "freee_api_request", side_effect=responses) as request,
    ):
        result = partners.create_or_find_partner("株式会社テストー", "T-009", force=True)
    assert result["status"] == "created"
    assert request.call_args_list[1].kwargs["json_body"]["code"] == "T-009"


def test_batch_registration_targets_every_unfinished_item():
    items = [
        {"id": 1, "status": "pending"},
        {"id": 2, "status": "waiting_receipt"},
        {"id": 3, "status": "error"},
        {"id": 4, "status": "pending_update"},
        {"id": 5, "status": "registered"},
        {"id": 6, "status": "manual"},
        {"id": 7, "status": "excluded"},
        {"id": 8, "status": "registering"},
    ]
    assert _batch_candidate_ids(items) == [1, 2, 3, 4]


def test_nav_bootstrap_does_not_overwrite_admin_edits():
    source = inspect.getsource(repository.ensure_nav_item)
    assert "UPDATE mfu_nav_items SET label" not in source
    assert "ON DUPLICATE KEY UPDATE label=VALUES(label)" not in source
