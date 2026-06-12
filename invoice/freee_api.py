from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.freee_api import services as freee_services
from app.utils.db import get_db

from .utils import now_jst


def _tax_code_for_item(item: dict, settings: dict) -> int:
    category = item.get("tax_category") or "tax10"
    if category == "tax8":
        return int(settings["tax_code_8"])
    if category == "nontax":
        return int(settings["tax_code_nontax"])
    return int(settings["tax_code"])


def _partner_name_for_invoice(invoice: dict, contact: dict | None) -> str:
    return (
        (contact or {}).get("freee_partner_name")
        or (contact or {}).get("name")
        or invoice.get("contact_name_snapshot")
        or ""
    ).strip()


def _format_freee_quantity(value) -> str:
    try:
        quantity = Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return str(value or "").strip()
    if quantity == quantity.to_integral_value():
        return str(int(quantity))
    return format(quantity.normalize(), "f")


def _format_freee_description(item: dict, invoice: dict) -> str:
    name = (item.get("item_name") or invoice.get("subject") or invoice.get("invoice_no") or "").strip()
    quantity = _format_freee_quantity(item.get("quantity"))
    unit_name = (item.get("unit_name") or "").strip()
    unit_price = int(item.get("unit_price_yen") or 0)
    parts = [name]
    if quantity or unit_name:
        parts.append(f"{quantity}{unit_name}".strip())
    parts.append(f"単価{unit_price}円")
    return "　".join(part for part in parts if part)


def _save_contact_freee_partner(contact_id: int, partner: dict) -> None:
    db = get_db()
    cur = db.cursor()
    now = now_jst()
    cur.execute(
        """
        UPDATE invoice_contacts
        SET freee_partner_id = %s,
            freee_partner_code = %s,
            freee_partner_name = COALESCE(NULLIF(freee_partner_name, ''), %s),
            updated_at = %s
        WHERE id = %s
        """,
        (
            partner.get("id"),
            partner.get("code"),
            partner.get("name") or partner.get("display_name"),
            now,
            contact_id,
        ),
    )
    db.commit()
    db.close()


def resolve_invoice_freee_partner(invoice: dict, contact: dict | None, company_id: int) -> int:
    if contact and contact.get("freee_partner_id"):
        return int(contact["freee_partner_id"])
    name = _partner_name_for_invoice(invoice, contact)
    if not name:
        raise RuntimeError("freee取引先名を決定できませんでした。請求先名を確認してください。")

    data = freee_services.freee_api_request(
        "GET",
        "/api/1/partners",
        params={"company_id": company_id, "keyword": name},
    )
    partners = freee_services.freee_list_from_response(data, "partners")
    partner = next((item for item in partners if str(item.get("name") or "") == name), None)
    if not partner and partners:
        partner = partners[0]
    if not partner:
        created = freee_services.freee_api_request(
            "POST",
            "/api/1/partners",
            json_body={"company_id": company_id, "name": name},
        )
        partner = created.get("partner") if isinstance(created, dict) else None
        if not partner:
            partner = created
    if not partner or not partner.get("id"):
        raise RuntimeError("freee取引先の検索/作成に失敗しました。")
    if contact and contact.get("id"):
        _save_contact_freee_partner(int(contact["id"]), partner)
    return int(partner["id"])


def build_invoice_freee_deal_payload(invoice: dict, contact: dict | None, settings: dict) -> dict:
    company_id = int(settings["company_id"])
    partner_id = resolve_invoice_freee_partner(invoice, contact, company_id)
    details = []
    for item in invoice.get("items") or []:
        if item.get("row_type") == "memo":
            memo_text = (item.get("memo_text") or "").strip()
            if not memo_text:
                continue
            amount = 0
            tax_code = int(settings["tax_code_nontax"])
            description = memo_text
        else:
            amount = int(item.get("line_total_yen") or 0)
            if amount <= 0:
                continue
            tax_code = _tax_code_for_item(item, settings)
            description = _format_freee_description(item, invoice)
        details.append(
            {
                "account_item_id": int(settings["account_item_id"]),
                "tax_code": tax_code,
                "amount": amount,
                "description": description,
            }
        )
    if not details:
        raise RuntimeError("freeeに登録できる請求明細がありません。")
    payload = {
        "company_id": company_id,
        "issue_date": invoice["issue_date"].isoformat(),
        "due_date": invoice["due_date"].isoformat(),
        "type": "income",
        "ref_number": invoice.get("invoice_no") or f"invoice-{invoice['id']}",
        "partner_id": partner_id,
        "details": details,
        "payments": [
            {
                "date": invoice["due_date"].isoformat(),
                "from_walletable_type": settings["walletable_type"],
                "from_walletable_id": int(settings["walletable_id"]),
                "amount": int(invoice.get("total_yen") or 0),
            }
        ],
    }
    return payload


def invoice_needs_freee_resync(invoice: dict) -> bool:
    if not invoice.get("freee_deal_id"):
        return False
    synced_at = invoice.get("freee_api_synced_at")
    updated_at = invoice.get("updated_at")
    if not synced_at:
        return True
    return bool(updated_at and updated_at > synced_at)


def _is_missing_freee_deal_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        "指定された取引は存在しません" in message
        or "The specified deal does not exist" in message
        or "HTTP 404" in message
    )


def _freee_deal_exists(deal_id: int, company_id: int) -> bool:
    try:
        freee_services.freee_api_request(
            "GET",
            f"/api/1/deals/{deal_id}",
            params={"company_id": company_id},
        )
        return True
    except Exception as exc:
        if _is_missing_freee_deal_error(exc):
            return False
        raise


def _create_invoice_freee_deal(payload: dict) -> int:
    data = freee_services.freee_api_request("POST", "/api/1/deals", json_body=payload)
    deal = data.get("deal") if isinstance(data, dict) else None
    deal_id = (deal or {}).get("id") or data.get("id")
    if not deal_id:
        raise RuntimeError("freee deal id was not returned.")
    return int(deal_id)


def _sync_invoice_freee_payment(deal_id: int, payload: dict) -> None:
    payment = dict((payload.get("payments") or [])[0])
    payment["company_id"] = payload["company_id"]
    data = freee_services.freee_api_request(
        "GET",
        f"/api/1/deals/{deal_id}",
        params={"company_id": payload["company_id"]},
    )
    deal = data.get("deal") if isinstance(data, dict) else {}
    payments = (deal or {}).get("payments") or []
    if payments and payments[0].get("id"):
        freee_services.freee_api_request(
            "PUT",
            f"/api/1/deals/{deal_id}/payments/{payments[0]['id']}",
            json_body=payment,
        )
        return
    freee_services.freee_api_request("POST", f"/api/1/deals/{deal_id}/payments", json_body=payment)


def mark_invoice_freee_error(invoice_id: int, message: str) -> None:
    db = get_db()
    cur = db.cursor()
    now = now_jst()
    cur.execute(
        """
        UPDATE invoice_headers
        SET freee_api_status = 'error',
            freee_api_error = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (freee_services.sanitize_freee_error(message), now, invoice_id),
    )
    db.commit()
    db.close()


def sync_invoice_to_freee(invoice: dict, contact: dict | None) -> dict:
    settings = freee_services.get_freee_deal_settings(freee_services.INVOICE_INTEGRATION_KEY)
    settings_error = freee_services.validate_freee_deal_settings(settings)
    if settings_error:
        raise RuntimeError(settings_error)
    if not freee_services.load_freee_token_row():
        raise RuntimeError("freeeに接続されていません。先にfreee接続を行ってください。")

    if invoice.get("freee_deal_id") and not invoice_needs_freee_resync(invoice):
        deal_id = int(invoice["freee_deal_id"])
        if _freee_deal_exists(deal_id, int(settings["company_id"])):
            return {"status": "skipped_already_synced", "freee_deal_id": deal_id}

    payload = build_invoice_freee_deal_payload(invoice, contact, settings)
    deal_payload = dict(payload)
    deal_payload.pop("payments", None)
    db = None
    try:
        if invoice.get("freee_deal_id"):
            deal_id = int(invoice["freee_deal_id"])
            try:
                freee_services.freee_api_request("PUT", f"/api/1/deals/{deal_id}", json_body=deal_payload)
                _sync_invoice_freee_payment(deal_id, payload)
                status = "updated"
            except Exception as exc:
                if not _is_missing_freee_deal_error(exc):
                    raise
                deal_id = _create_invoice_freee_deal(payload)
                status = "recreated"
        else:
            deal_id = _create_invoice_freee_deal(payload)
            status = "synced"

        db = get_db()
        cur = db.cursor()
        now = now_jst()
        if status == "updated":
            freee_timestamps_sql = """
                freee_api_synced_at = %s,
                freee_api_modified_at = %s,
            """
            freee_timestamps_values = (now, now)
        else:
            freee_timestamps_sql = """
                freee_api_synced_at = %s,
                freee_api_registered_at = %s,
                freee_api_modified_at = NULL,
            """
            freee_timestamps_values = (now, now)
        cur.execute(
            f"""
            UPDATE invoice_headers
            SET freee_deal_id = %s,
                {freee_timestamps_sql}
                freee_api_status = 'synced',
                freee_api_error = NULL,
                updated_at = updated_at
            WHERE id = %s
            """,
            (deal_id, *freee_timestamps_values, invoice["id"]),
        )
        db.commit()
        db.close()
        return {"status": status, "freee_deal_id": deal_id}
    except Exception as exc:
        if db:
            db.close()
        mark_invoice_freee_error(int(invoice["id"]), str(exc))
        raise
