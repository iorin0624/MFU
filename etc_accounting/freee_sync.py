from __future__ import annotations

import os
import hashlib
import io
from datetime import datetime

from app.freee_api import services as freee_services

from .parser import is_provisional_record
from .pdf_metadata import extract_pdf_metadata, parse_invoice_registration_number
from .repository import (
    claim_registration,
    claim_registered_update,
    get_record,
    get_registration_mapping,
    update_pdf_metadata,
    update_registration,
)


INTEGRATION_KEY = "etc"


def _settings() -> dict:
    settings = freee_services.get_freee_deal_settings(INTEGRATION_KEY)
    error = freee_services.validate_freee_deal_settings(settings)
    if error:
        raise RuntimeError(error)
    return settings


def _route_point(name: object, passed_at: object, missing_label: str) -> str:
    label = str(name or "").strip() or missing_label
    if isinstance(passed_at, datetime):
        return f"{label} {passed_at.month}/{passed_at.day} {passed_at:%H:%M}"
    return label


def _description(record: dict) -> str:
    entry_at = record.get("entry_at")
    exit_at = record.get("exit_at")
    entry = _route_point(record.get("entry_ic"), entry_at, "入口記録なし")
    exit_ = _route_point(record.get("exit_ic"), exit_at, "出口記録なし")
    description = f"ETC通行料金 {entry} → {exit_}"
    if not isinstance(entry_at, datetime) and not isinstance(exit_at, datetime):
        used_at = record.get("used_at")
        if isinstance(used_at, datetime):
            description = f"{description} {used_at:%H:%M}"
    return description


_parse_invoice_registration_number = parse_invoice_registration_number


def _invoice_registration_number(record: dict) -> str:
    stored = str(record.get("invoice_registration_number") or "").strip().upper()
    if stored:
        return stored
    metadata = extract_pdf_metadata(str(record.get("pdf_path") or ""))
    if record.get("id"):
        update_pdf_metadata(int(record["id"]), metadata["registration_number"], metadata["issuer_name"])
        record["invoice_registration_number"] = metadata["registration_number"]
        record["invoice_issuer_name"] = metadata["issuer_name"]
    return metadata["registration_number"]


def _upload_pdf(
    record: dict,
    company_id: int,
    *,
    upload_name: str | None = None,
    unique_content: bool = False,
) -> int:
    path = str(record.get("pdf_path") or "")
    if not path or not os.path.isfile(path):
        raise RuntimeError("ETC利用証明書PDFが見つかりません。再取得してください。")
    with open(path, "rb") as source:
        pdf = source
        if unique_content:
            original = source.read()
            source_hash = hashlib.sha256(original).hexdigest()
            marker = (
                f"\n% MFU re-registration source-sha256={source_hash} "
                f"nonce={datetime.now():%Y%m%d%H%M%S%f}\n"
            ).encode("ascii")
            eof_index = original.rfind(b"%%EOF")
            upload_bytes = (
                original[:eof_index] + marker + original[eof_index:]
                if eof_index >= 0
                else original + marker
            )
            pdf = io.BytesIO(upload_bytes)
        data = freee_services.freee_api_multipart_request(
            "POST",
            "/api/1/receipts",
            data={
                "company_id": str(company_id),
                "description": _description(record)[:255],
                "receipt_metadatum_partner_name": "ETC利用照会サービス",
                "receipt_metadatum_issue_date": record["used_at"].strftime("%Y-%m-%d"),
                "receipt_metadatum_amount": str(int(record["amount"])),
                "qualified_invoice": "qualified",
                "document_type": "receipt",
            },
            files={"receipt": (upload_name or os.path.basename(path), pdf, "application/pdf")},
        )
    receipt = data.get("receipt") if isinstance(data, dict) else None
    receipt_id = (receipt or {}).get("id") or (data.get("id") if isinstance(data, dict) else None)
    if not receipt_id:
        raise RuntimeError("freeeから証憑ファイルIDが返されませんでした。")
    return int(receipt_id)


def _update_receipt_invoice_metadata(
    record: dict,
    company_id: int,
    receipt_id: int,
    registration_number: str | None = None,
) -> str:
    registration_number = registration_number or _invoice_registration_number(record)
    freee_services.freee_api_request(
        "PUT",
        f"/api/1/receipts/{int(receipt_id)}",
        json_body={
            "company_id": int(company_id),
            "description": _description(record)[:255],
            "receipt_metadatum": {
                "partner_name": "ETC利用照会サービス",
                "issue_date": record["used_at"].strftime("%Y-%m-%d"),
                "amount": int(record["amount"]),
            },
            "qualified_invoice": "qualified",
            "invoice_registration_number": registration_number,
            "document_type": "receipt",
        },
    )
    return registration_number


def _deal_payload(record: dict, settings: dict, receipt_id: int, mapping: dict | None = None) -> dict:
    used_at = record["used_at"]
    issue_date = used_at.strftime("%Y-%m-%d")
    amount = int(record["amount"])
    detail = {
        "account_item_id": int(settings["account_item_id"]),
        "tax_code": int(settings["tax_code"]),
        "amount": amount,
        "description": _description(record),
    }
    if mapping and mapping.get("item_id"):
        detail["item_id"] = int(mapping["item_id"])
    payload = {
        "company_id": int(settings["company_id"]),
        "issue_date": issue_date,
        "due_date": issue_date,
        "type": "expense",
        "ref_number": f"MFU-ETC-{record['id']}",
        "receipt_ids": [int(receipt_id)],
        "details": [detail],
    }
    if mapping and mapping.get("partner_id"):
        payload["partner_id"] = int(mapping["partner_id"])
    elif settings.get("partner_id"):
        payload["partner_id"] = int(settings["partner_id"])
    elif settings.get("partner_code"):
        payload["partner_code"] = str(settings["partner_code"])
    if (settings.get("deal_payment_mode") or "settled") == "settled":
        payload["payments"] = [
            {
                "date": issue_date,
                "from_walletable_type": settings["walletable_type"],
                "from_walletable_id": int(settings["walletable_id"]),
                "amount": amount,
            }
        ]
    return payload


def _deal_update_payload(
    record: dict,
    settings: dict,
    receipt_id: int,
    mapping: dict,
    detail_id: int,
    existing_receipt_ids: list[int] | None = None,
) -> dict:
    payload = _deal_payload(record, settings, receipt_id, mapping)
    payload.pop("payments", None)  # PUT /deals/{id} では支払行を操作できない。
    payload["details"][0]["id"] = int(detail_id)
    receipt_ids = {int(receipt_id)}
    receipt_ids.update(int(value) for value in (existing_receipt_ids or []) if int(value or 0) > 0)
    payload["receipt_ids"] = sorted(receipt_ids)
    return payload


def _deal_receipt_ids(deal: dict) -> list[int]:
    values = []
    for row in deal.get("receipt_ids") or []:
        value = row.get("id") if isinstance(row, dict) else row
        try:
            if int(value or 0) > 0:
                values.append(int(value))
        except (TypeError, ValueError):
            continue
    return values


def _is_freee_not_found(exc: Exception) -> bool:
    message = str(exc)
    return any(
        marker in message
        for marker in (
            "HTTP 404",
            "既に削除されています",
            "すでに削除されています",
            "存在しないか既に削除",
        )
    )


def _ensure_receipt_for_update(
    record: dict,
    company_id: int,
    registration_number: str,
    *,
    force_new: bool = False,
) -> int:
    receipt_id = int(record.get("freee_receipt_id") or 0)
    if receipt_id and not force_new:
        try:
            _update_receipt_invoice_metadata(record, company_id, receipt_id, registration_number)
            return receipt_id
        except Exception as exc:
            if not _is_freee_not_found(exc):
                raise
    path = str(record.get("pdf_path") or "")
    stem, extension = os.path.splitext(os.path.basename(path))
    upload_name = f"{stem}_mfu-reregister-{int(record['id'])}-{datetime.now():%Y%m%d%H%M%S}{extension or '.pdf'}"
    try:
        receipt_id = _upload_pdf(
            record,
            company_id,
            upload_name=upload_name,
            unique_content=True,
        )
    except Exception as exc:
        raise RuntimeError(f"削除済み証憑の再アップロードに失敗しました: {exc}") from exc
    if receipt_id == int(record.get("freee_receipt_id") or 0):
        raise RuntimeError("freeeが削除済み証憑と同じIDを返したため、再登録を停止しました。")
    update_registration(int(record["id"]), freee_receipt_id=receipt_id)
    record["freee_receipt_id"] = receipt_id
    _update_receipt_invoice_metadata(record, company_id, receipt_id, registration_number)
    return receipt_id


def update_registered_record(record_id: int) -> dict:
    record = get_record(record_id)
    if not record:
        raise LookupError("ETC明細が見つかりません。")
    if record.get("source_state") == "deleted":
        raise RuntimeError("この明細はETC照会サービスから削除されているため更新できません。")
    if is_provisional_record(record):
        raise RuntimeError("この明細は料金確認中のため更新できません。")
    if not record.get("freee_deal_id") or record.get("status") != "registered":
        raise RuntimeError("freee登録済みの明細ではありません。")

    registration_number = _invoice_registration_number(record)
    settings = _settings()
    company_id = int(settings["company_id"])
    mapping = get_registration_mapping(company_id, registration_number)
    if not mapping:
        raise RuntimeError(
            f"登録番号 {registration_number} のfreee取引先・品目が未設定です。設定画面で登録してください。"
        )
    if not claim_registered_update(record_id):
        raise RuntimeError("この明細は更新処理中です。")

    try:
        old_deal_id = int(record["freee_deal_id"])
        try:
            response = freee_services.freee_api_request(
                "GET",
                f"/api/1/deals/{old_deal_id}",
                params={"company_id": company_id},
            )
            deal = response.get("deal") if isinstance(response, dict) else None
            deal = deal or (response if isinstance(response, dict) else {})
        except Exception as exc:
            if not _is_freee_not_found(exc):
                raise
            deal = None

        if deal is not None:
            if str(deal.get("type") or "") != "expense":
                raise RuntimeError("freee側の取引が支出取引ではないため、安全のため更新を停止しました。")
            details = deal.get("details") or []
            if len(details) != 1 or not details[0].get("id"):
                raise RuntimeError("freee側の明細行が変更されているため、安全のため更新を停止しました。")

        receipt_id = _ensure_receipt_for_update(
            record,
            company_id,
            registration_number,
            force_new=deal is None,
        )
        if deal is None:
            response = freee_services.freee_api_request(
                "POST",
                "/api/1/deals",
                json_body=_deal_payload(record, settings, receipt_id, mapping),
            )
            new_deal = response.get("deal") if isinstance(response, dict) else None
            new_deal_id = (new_deal or {}).get("id") or (response.get("id") if isinstance(response, dict) else None)
            if not new_deal_id:
                raise RuntimeError("freeeから再登録した取引IDが返されませんでした。")
            update_registration(
                record_id,
                status="registered",
                freee_receipt_id=receipt_id,
                freee_deal_id=int(new_deal_id),
                freee_error=None,
                registered_at=datetime.now(),
            )
            return {"status": "reregistered", "deal_id": int(new_deal_id), "previous_deal_id": old_deal_id}

        freee_services.freee_api_request(
            "PUT",
            f"/api/1/deals/{old_deal_id}",
            json_body=_deal_update_payload(
                record,
                settings,
                receipt_id,
                mapping,
                int(details[0]["id"]),
                _deal_receipt_ids(deal),
            ),
        )
        update_registration(record_id, status="registered", freee_error=None)
        return {"status": "updated", "deal_id": old_deal_id, "receipt_id": receipt_id}
    except Exception as exc:
        update_registration(
            record_id,
            status="registered",
            freee_error=freee_services.sanitize_freee_error(str(exc)),
        )
        raise


def register_record(record_id: int) -> dict:
    record = get_record(record_id)
    if not record:
        raise LookupError("ETC明細が見つかりません。")
    if record.get("source_state") == "deleted":
        raise RuntimeError("この明細はETC照会サービスから削除されているため登録できません。")
    if is_provisional_record(record):
        raise RuntimeError("この明細は料金確認中のため、料金確定後にfreeeへ登録してください。")
    if record.get("freee_deal_id"):
        return {"status": "already_registered", "deal_id": int(record["freee_deal_id"])}
    registration_number = _invoice_registration_number(record)
    settings = _settings()
    mapping = get_registration_mapping(int(settings["company_id"]), registration_number)
    if not mapping:
        raise RuntimeError(
            f"登録番号 {registration_number} のfreee取引先・品目が未設定です。設定画面で登録してください。"
        )
    if not claim_registration(record_id):
        raise RuntimeError("この明細は登録処理中です。")
    try:
        company_id = int(settings["company_id"])
        receipt_id = int(record.get("freee_receipt_id") or 0)
        if not receipt_id:
            receipt_id = _upload_pdf(record, company_id)
            update_registration(record_id, freee_receipt_id=receipt_id)
            record["freee_receipt_id"] = receipt_id
        _update_receipt_invoice_metadata(record, company_id, receipt_id, registration_number)
        response = freee_services.freee_api_request(
            "POST",
            "/api/1/deals",
            json_body=_deal_payload(record, settings, receipt_id, mapping),
        )
        deal = response.get("deal") if isinstance(response, dict) else None
        deal_id = (deal or {}).get("id") or (response.get("id") if isinstance(response, dict) else None)
        if not deal_id:
            raise RuntimeError("freeeから取引IDが返されませんでした。")
        update_registration(
            record_id,
            status="registered",
            freee_receipt_id=receipt_id,
            freee_deal_id=int(deal_id),
            freee_error=None,
            registered_at=datetime.now(),
        )
        return {"status": "registered", "deal_id": int(deal_id), "receipt_id": receipt_id}
    except Exception as exc:
        update_registration(record_id, status="error", freee_error=freee_services.sanitize_freee_error(str(exc)))
        raise
