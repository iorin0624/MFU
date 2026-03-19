from __future__ import annotations

from datetime import datetime
from typing import Any

from app.utils.db import get_db

from .token_service import build_payout_access_url, create_payout_access_token


def build_invoice_payout_token_memo(invoice: dict[str, Any]) -> str:
    invoice_no = str(invoice.get("invoice_no") or "").strip()
    if not invoice_no:
        raise ValueError("invoice_no が空のため payout token 用メモを生成できません。")

    subject = str(invoice.get("subject") or "").strip()
    if not subject:
        raise ValueError("subject が空のため payout token 用メモを生成できません。")

    return f"{invoice_no}　{subject}"


def issue_payout_access_token_for_invoice(
    invoice: dict[str, Any],
    *,
    created_by_admin: str | None = None,
    expires_at: datetime | None = None,
) -> dict[str, Any]:
    memo = build_invoice_payout_token_memo(invoice)
    db = get_db()
    try:
        created = create_payout_access_token(
            db,
            memo=memo,
            issued_via="internal",
            issued_by_app="invoice",
            created_by_admin=created_by_admin,
            expires_at=expires_at,
        )
    finally:
        db.close()

    access_url = str(created.get("access_url") or "").strip()
    token = str(created.get("token") or "").strip()
    if not access_url and token:
        access_url = build_payout_access_url(token)

    return {
        "id": created.get("id"),
        "memo": created.get("memo") or memo,
        "token": token,
        "token_preview": created.get("token_preview"),
        "access_url": access_url,
        "issued_by_app": created.get("issued_by_app") or "invoice",
        "created_at": created.get("created_at"),
    }
