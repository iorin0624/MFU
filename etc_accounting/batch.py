from __future__ import annotations

import os

from app.freee_api import services as freee_services

from .freee_sync import INTEGRATION_KEY, register_record
from .parser import is_provisional_record
from .repository import (
    claim_batch_job,
    finish_batch_job,
    get_batch_job,
    get_batch_items,
    get_record,
    get_registration_mapping,
    update_batch_item,
)
from app.utils.realtime import emit_admin_event


MAX_BATCH_SIZE = 50


def _emit_batch_progress(job_id: str) -> None:
    job = get_batch_job(job_id)
    if not job:
        return
    items = get_batch_items(job_id)
    emit_admin_event(
        "etc_batch_job_update",
        {
            "job": {
                "id": job["id"],
                "status": job["status"],
                "total_count": int(job.get("total_count") or 0),
                "success_count": int(job.get("success_count") or 0),
                "failure_count": int(job.get("failure_count") or 0),
                "skipped_count": int(job.get("skipped_count") or 0),
                "total_amount": int(job.get("total_amount") or 0),
                "error_text": job.get("error_text") or "",
            },
            "items": [
                {
                    "id": int(item["id"]),
                    "record_id": int(item["record_id"]),
                    "status": item["status"],
                    "deal_id": item.get("freee_deal_id"),
                    "error": item.get("error_text") or "",
                }
                for item in items
            ],
        },
        room=f"etc-batch:{job_id}",
    )


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
    _emit_batch_progress(job_id)
    try:
        settings = freee_services.get_freee_deal_settings(INTEGRATION_KEY) or {}
        company_id = int(settings.get("company_id") or 0) or None
        for item in get_batch_items(job_id):
            item_id = int(item["id"])
            update_batch_item(item_id, status="running")
            _emit_batch_progress(job_id)
            record = get_record(int(item["record_id"]))
            if not record:
                update_batch_item(item_id, status="skipped", error="明細が見つかりません")
                _emit_batch_progress(job_id)
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
                _emit_batch_progress(job_id)
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
            _emit_batch_progress(job_id)
        result = finish_batch_job(job_id)
        _emit_batch_progress(job_id)
        return result
    except Exception as exc:
        result = finish_batch_job(
            job_id,
            error=freee_services.sanitize_freee_error(str(exc)),
        )
        _emit_batch_progress(job_id)
        return result
