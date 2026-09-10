from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageOps

from app.freee_api import services as freee_services

from .repository import (
    claim_registration,
    get_month_item,
    list_attachments,
    set_registration,
    update_attachment_freee,
)


def _company_id() -> int:
    settings = freee_services.get_freee_common_settings()
    if not settings or not settings.get("company_id"):
        raise RuntimeError("freee共通設定で事業所を選択してください。")
    return int(settings["company_id"])


def _ref_number(item: dict) -> str:
    return f"MFU-RECURRING-{int(item['master_id'])}-{item['target_month'].replace('-', '')}"


def _description(item: dict) -> str:
    return f"{item['name']}（{item['target_month']}）"


def _attachment_pdf(attachment: dict) -> tuple[str, io.BufferedReader | io.BytesIO]:
    path = Path(str(attachment["file_path"]))
    if not path.is_file():
        raise RuntimeError(f"添付ファイル「{attachment['original_name']}」が見つかりません。")
    if attachment["mime_type"] == "application/pdf":
        return path.name, path.open("rb")

    try:
        if path.suffix.lower() in {".heic", ".heif"}:
            from pillow_heif import register_heif_opener

            register_heif_opener()
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            output = io.BytesIO()
            image.save(output, format="PDF", resolution=150.0)
            output.seek(0)
    except Exception as exc:
        raise RuntimeError(f"画像「{attachment['original_name']}」をPDFへ変換できませんでした。") from exc
    return f"{path.stem}.pdf", output


def _upload_attachment(item: dict, attachment: dict, company_id: int) -> int:
    upload_name, stream = _attachment_pdf(attachment)
    try:
        data = freee_services.freee_api_multipart_request(
            "POST",
            "/api/1/receipts",
            data={
                "company_id": str(company_id),
                "description": _description(item)[:255],
                "receipt_metadatum_partner_name": item["name"][:255],
                "receipt_metadatum_issue_date": item["issue_date"].strftime("%Y-%m-%d"),
                "receipt_metadatum_amount": str(int(item["actual_amount"])),
                "document_type": "receipt",
            },
            files={"receipt": (upload_name, stream, "application/pdf")},
        )
    finally:
        stream.close()
    receipt = data.get("receipt") if isinstance(data, dict) else None
    receipt_id = (receipt or {}).get("id") or (data.get("id") if isinstance(data, dict) else None)
    if not receipt_id:
        raise RuntimeError("freeeから証憑ファイルIDが返されませんでした。")
    update_attachment_freee(int(attachment["id"]), int(receipt_id), None)
    return int(receipt_id)


def _receipt_ids(item: dict, company_id: int) -> list[int]:
    result = []
    for attachment in list_attachments(int(item["id"])):
        receipt_id = int(attachment.get("freee_receipt_id") or 0)
        if not receipt_id:
            try:
                receipt_id = _upload_attachment(item, attachment, company_id)
            except Exception as exc:
                update_attachment_freee(
                    int(attachment["id"]),
                    None,
                    freee_services.sanitize_freee_error(str(exc)),
                )
                raise
        result.append(receipt_id)
    return result


def _deal_payload(item: dict, company_id: int, receipt_ids: list[int]) -> dict:
    issue_date = item["issue_date"].strftime("%Y-%m-%d")
    detail = {
        "account_item_id": int(item["account_item_id"]),
        "tax_code": int(item["tax_code"]),
        "amount": int(item["actual_amount"]),
        "description": _description(item),
    }
    if item.get("item_id"):
        detail["item_id"] = int(item["item_id"])
    payload = {
        "company_id": company_id,
        "issue_date": issue_date,
        "due_date": issue_date,
        "type": "expense",
        "ref_number": _ref_number(item),
        "receipt_ids": receipt_ids,
        "details": [detail],
    }
    if item.get("partner_id"):
        payload["partner_id"] = int(item["partner_id"])
    if item.get("payment_mode") == "settled":
        if not item.get("walletable_type") or not item.get("walletable_id"):
            raise RuntimeError("支払済み取引の決済口座が設定されていません。")
        payload["payments"] = [{
            "date": issue_date,
            "from_walletable_type": item["walletable_type"],
            "from_walletable_id": int(item["walletable_id"]),
            "amount": int(item["actual_amount"]),
        }]
    return payload


def _find_deal(item: dict, company_id: int) -> dict | None:
    issue_date = item["issue_date"].strftime("%Y-%m-%d")
    data = freee_services.freee_api_request(
        "GET",
        "/api/1/deals",
        params={
            "company_id": company_id,
            "issue_date_start": issue_date,
            "issue_date_end": issue_date,
            "limit": 100,
        },
    )
    for deal in data.get("deals") or []:
        if str(deal.get("ref_number") or "") == _ref_number(item):
            return deal
    return None


def _deal_by_id(deal_id: int, company_id: int) -> dict:
    data = freee_services.freee_api_request(
        "GET", f"/api/1/deals/{deal_id}", params={"company_id": company_id}
    )
    return data.get("deal") or data


def _attach_to_existing(item: dict, deal: dict, company_id: int, receipt_ids: list[int]) -> int:
    if str(deal.get("type") or "") != "expense":
        raise RuntimeError("指定されたfreee取引は支出取引ではありません。")
    details = deal.get("details") or []
    if len(details) != 1 or not details[0].get("id"):
        raise RuntimeError("複数明細のfreee取引には安全のため自動添付できません。")
    payload = _deal_payload(item, company_id, receipt_ids)
    payload.pop("payments", None)
    payload["details"][0]["id"] = int(details[0]["id"])
    existing_ids = set()
    for value in deal.get("receipt_ids") or []:
        raw = value.get("id") if isinstance(value, dict) else value
        if raw:
            existing_ids.add(int(raw))
    payload["receipt_ids"] = sorted(existing_ids | set(receipt_ids))
    deal_id = int(deal["id"])
    freee_services.freee_api_request("PUT", f"/api/1/deals/{deal_id}", json_body=payload)
    return deal_id


def register_month(month_id: int) -> dict:
    item = get_month_item(month_id)
    if not item:
        raise LookupError("定期経費が見つかりません。")
    if item.get("freee_deal_id") and item.get("status") != "pending_update":
        return {"status": "already_registered", "deal_id": int(item["freee_deal_id"])}
    if item.get("status") == "excluded":
        raise RuntimeError("今月は対象外に設定されています。")
    if item.get("actual_amount") is None or int(item["actual_amount"]) < 0:
        raise RuntimeError("実際の金額を入力してください。")
    attachments = list_attachments(month_id)
    if item.get("receipt_required") and not attachments:
        raise RuntimeError("この経費はPDF・領収書の添付が必須です。")
    if item.get("registration_mode") == "manual":
        set_registration(month_id, status="manual")
        return {"status": "manual"}
    if not claim_registration(month_id):
        raise RuntimeError("この経費は登録処理中か、すでに処理済みです。")
    try:
        company_id = _company_id()
        receipt_ids = _receipt_ids(item, company_id)
        if item.get("freee_deal_id"):
            deal_id = int(item["freee_deal_id"])
            deal_id = _attach_to_existing(item, _deal_by_id(deal_id, company_id), company_id, receipt_ids)
        elif item.get("registration_mode") == "existing":
            deal_id = int(item.get("existing_deal_id") or 0)
            if not deal_id:
                raise RuntimeError("紐付けるfreee取引IDを入力してください。")
            deal_id = _attach_to_existing(item, _deal_by_id(deal_id, company_id), company_id, receipt_ids)
        else:
            existing = _find_deal(item, company_id)
            if existing:
                deal_id = _attach_to_existing(item, existing, company_id, receipt_ids)
            else:
                response = freee_services.freee_api_request(
                    "POST", "/api/1/deals", json_body=_deal_payload(item, company_id, receipt_ids)
                )
                deal = response.get("deal") if isinstance(response, dict) else None
                deal_id = int((deal or {}).get("id") or response.get("id") or 0)
                if not deal_id:
                    raise RuntimeError("freeeから取引IDが返されませんでした。")
        set_registration(month_id, status="registered", deal_id=deal_id)
        return {"status": "registered", "deal_id": deal_id, "receipt_ids": receipt_ids}
    except Exception as exc:
        set_registration(
            month_id,
            status="error",
            error=freee_services.sanitize_freee_error(str(exc)),
        )
        raise
