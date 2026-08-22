from __future__ import annotations

from email import policy
from email.parser import Parser
from unittest.mock import MagicMock, patch

from app import app as flask_app
from app.ticket_price_research import inbox_cli
from app.ticket_price_research.mail_service import (
    build_plain_body,
    send_disney_ticket_price_reply,
)
from app.utils.mail import send_mail


SAMPLE_PAYLOAD = {
    "ok": True,
    "cached": False,
    "fetched_at": "2026-07-30 12:34",
    "count": 1,
    "warnings": [],
    "items": [
        {
            "title": "東京ディズニーリゾート 1DAYパスポート",
            "detail_url": "https://example.jp/item",
            "price": 7900,
            "expiry_display": "2026年10月31日",
            "expiry_sort": "2026-10-31",
            "shop_name": "テスト店舗",
            "shop_url": "https://example.jp/shop",
        }
    ],
}


def test_plain_body_contains_ticket_summary():
    body = build_plain_body(SAMPLE_PAYLOAD)
    assert "東京ディズニーリゾート 1DAYパスポート" in body
    assert "¥7,900" in body
    assert "2026年10月31日" in body
    assert "PDFを添付" in body


def test_reply_uses_html_and_pdf_attachment():
    with flask_app.app_context(), patch(
        "app.ticket_price_research.mail_service.fetch_disney_ticket_items",
        return_value=SAMPLE_PAYLOAD,
    ), patch(
        "app.ticket_price_research.mail_service.render_disney_ticket_pdf",
        return_value=b"%PDF-test",
    ), patch(
        "app.ticket_price_research.mail_service.send_mail"
    ) as mocked_send:
        result = send_disney_ticket_price_reply("allowed@example.jp")

    assert result["count"] == 1
    args, kwargs = mocked_send.call_args
    assert args[0] == "allowed@example.jp"
    assert "¥7,900" in args[2]
    assert "text" not in kwargs
    assert "¥7,900" in kwargs["html_body"]
    assert kwargs["attachments"][0]["content_type"] == "application/pdf"
    assert kwargs["attachments"][0]["data"] == b"%PDF-test"


def test_send_mail_builds_mixed_alternative_and_pdf(monkeypatch):
    smtp = MagicMock()
    smtp.ehlo.return_value = (250, b"ok")
    smtp.starttls.return_value = (220, b"ready")
    smtp.sendmail.return_value = {}
    monkeypatch.setattr(
        "app.utils.mail._load_smtp_settings",
        lambda: ("smtp.example.jp", 587, "user", "password"),
    )
    monkeypatch.setattr("app.utils.mail.smtplib.SMTP", lambda **_kwargs: smtp)
    monkeypatch.setattr("app.utils.mail.record_mail_submission", lambda **_kwargs: None)
    monkeypatch.setattr("app.utils.mail.write_smtp_log", lambda _line: None)

    send_mail(
        "allowed@example.jp",
        "件名",
        "プレーン本文",
        html_body="<p>HTML本文</p>",
        attachments=[
            {
                "filename": "ticket.pdf",
                "data": b"%PDF-test",
                "content_type": "application/pdf",
            }
        ],
        append_signature=False,
    )

    raw_message = smtp.sendmail.call_args.args[2]
    message = Parser(policy=policy.default).parsestr(raw_message)
    assert message.get_content_type() == "multipart/mixed"
    parts = list(message.iter_parts())
    assert parts[0].get_content_type() == "multipart/alternative"
    alternatives = list(parts[0].iter_parts())
    assert [part.get_content_type() for part in alternatives] == [
        "text/plain",
        "text/html",
    ]
    assert alternatives[0].get_content().strip() == "プレーン本文"
    assert "HTML本文" in alternatives[1].get_content()
    assert parts[1].get_content_type() == "application/pdf"
    assert parts[1].get_filename() == "ticket.pdf"


def test_inbox_sender_prefers_return_path():
    message = Parser(policy=policy.default).parsestr(
        "Return-Path: <registered@example.jp>\n"
        "From: Display <spoofed@example.jp>\n"
        "Message-ID: <test@example.jp>\n\n"
    )
    assert inbox_cli._sender_from_message(message) == "registered@example.jp"
    assert inbox_cli._from_header_sender(message) == "spoofed@example.jp"


def test_inbox_rejects_auto_submitted_message():
    message = Parser(policy=policy.default).parsestr(
        "Return-Path: <registered@example.jp>\n"
        "Auto-Submitted: auto-replied\n\n"
    )
    assert inbox_cli._is_automatic_message(message, "registered@example.jp")
