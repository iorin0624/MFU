from __future__ import annotations

from datetime import datetime

from flask import current_app

from app.utils.mail import send_mail

from .pdf import render_disney_ticket_pdf
from .services import fetch_disney_ticket_items


def pdf_filename(now: datetime | None = None) -> str:
    timestamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"disney_ticket_prices_{timestamp}.pdf"


def build_plain_body(payload: dict) -> str:
    lines = [
        "東京ディズニーリゾート チケット価格一覧",
        "",
        f"取得日時: {payload.get('fetched_at') or '-'}",
        f"掲載件数: {int(payload.get('count') or 0)}件",
        "",
    ]
    for item in payload.get("items") or []:
        price = item.get("price")
        price_label = f"¥{int(price):,}" if price is not None else "不明"
        lines.extend(
            [
                str(item.get("title") or "商品名不明"),
                f"価格: {price_label}",
                f"有効期限: {item.get('expiry_display') or '不明'}",
                f"取扱店: {item.get('shop_name') or '不明'}",
                *([f"店舗所在地: {item.get('shop_area')}"] if item.get("shop_area") else []),
                f"商品詳細: {item.get('detail_url') or '-'}",
                "",
            ]
        )
    warnings = payload.get("warnings") or []
    if warnings:
        lines.extend(["取得時の注意:", *[f"- {warning}" for warning in warnings], ""])
    lines.extend(
        [
            "価格・在庫は変動します。購入前に各店舗ページで最新情報をご確認ください。",
            "本メールには同じ内容のPDFを添付しています。",
        ]
    )
    return "\n".join(lines)


def render_html_body(payload: dict) -> str:
    return current_app.jinja_env.get_template(
        "ticket_price_research/disney_email.html"
    ).render(payload=payload)


def send_disney_ticket_price_reply(recipient: str) -> dict:
    payload = fetch_disney_ticket_items()
    if not payload.get("ok") or not payload.get("items"):
        raise RuntimeError("チケット価格情報を取得できませんでした")
    pdf_bytes = render_disney_ticket_pdf(payload)
    send_mail(
        recipient,
        f"【MFU】ディズニーチケット価格一覧（{datetime.now():%Y年%m月%d日}）",
        build_plain_body(payload),
        html_body=render_html_body(payload),
        attachments=[
            {
                "filename": pdf_filename(),
                "data": pdf_bytes,
                "content_type": "application/pdf",
            }
        ],
        from_display_name="MFU チケット価格情報",
        mail_kind="ticket_price_pdf_reply",
        append_signature=False,
    )
    return payload
