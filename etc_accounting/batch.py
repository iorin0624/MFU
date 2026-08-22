from __future__ import annotations

import os

from app.freee_api import services as freee_services

from .freee_sync import INTEGRATION_KEY, register_record
from .parser import is_provisional_record
from .repository import (
    claim_batch_job,
    finish_batch_job,
    get_batch_items,
    get_record,
    get_registration_mapping,
    update_batch_item,
)


MAX_BATCH_SIZE = 50


def registration_eligibility(
    record: dict,
    *,
    company_id: int | None,
    mapping: dict | None,
    check_pdf_file: bool = True,
) -> tuple[bool, str]:
    if record.get("source_state") == "deleted":
        return False, "照会サービスから削除済み"
    if record.get("freee_deal_id") or record.get("status") == "registered":
        return False, "freee登録済み"
    if is_provisional_record(record):
        return False, "料金確認中"
    if record.get("status") not in {"pending", "error"}:
        return False, "別の登録処理を実行中"
    pdf_path = str(record.get("pdf_path") or "")
    if not pdf_path:
        return False, "PDF未取得"
    if check_pdf_file and not os.path.isfile(pdf_path):
        return False, "PDFファイルが見つかりません"
    registration_number = str(record.get("invoice_registration_number") or "").strip().upper()
    if not registration_number:
        return False, "登録番号未取得"
    if not company_id:
        return False, "freee事業所未設定"
    if not mapping or not mapping.get("partner_id") or not mapping.get("item_id"):
        return False, "取引先・品目未設定"
    return True, "登録可能"


def evaluate_records(records: list[dict], *, company_id: int | None) -> tuple[list[dict], list[dict]]:
    eligible: list[dict] = []
    excluded: list[dict] = []
    for record in records:
        registration_number = str(record.get("invoice_registration_number") or "").strip().upper()
        mapping = (
            get_registration_mapping(int(company_id), registration_number)
            if company_id and registration_number
            else None
        )
        record["registration_mapping"] = mapping
        allowed, reason = registration_eligibility(
            record,
            company_id=company_id,
            mapping=mapping,
        )
        record["batch_eligible"] = allowed
        record["batch_reason"] = reason
        (eligible if allowed else excluded).append(record)
    return eligible, excluded


def run_batch_job(job_id: str) -> dict | None:
    if not claim_batch_job(job_id):
        return None
    try:
        settings = freee_services.get_freee_deal_settings(INTEGRATION_KEY) or {}
        company_id = int(settings.get("company_id") or 0) or None
        for item in get_batch_items(job_id):
            item_id = int(item["id"])
            update_batch_item(item_id, status="running")
            record = get_record(int(item["record_id"]))
            if not record:
                update_batch_item(item_id, status="skipped", error="明細が見つかりません")
                continue
            registration_number = str(record.get("invoice_registration_number") or "").strip().upper()
            mapping = (
                get_registration_mapping(int(company_id), registration_number)
                if company_id and registration_number
                else None
            )
            allowed, reason = registration_eligibility(
                record,
                company_id=company_id,
                mapping=mapping,
            )
            if not allowed:
                update_batch_item(item_id, status="skipped", error=reason)
                continue
            try:
                result = register_record(int(record["id"]))
                deal_id = int(result.get("deal_id") or 0) or None
                update_batch_item(item_id, status="success", deal_id=deal_id)
            except Exception as exc:
                update_batch_item(
                    item_id,
                    status="failed",
                    error=freee_services.sanitize_freee_error(str(exc)),
                )
        return finish_batch_job(job_id)
    except Exception as exc:
        return finish_batch_job(
            job_id,
            error=freee_services.sanitize_freee_error(str(exc)),
        )
