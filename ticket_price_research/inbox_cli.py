from __future__ import annotations

import configparser
import hashlib
import imaplib
import json
import logging
import os
import ssl
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/etc/mfu/ticket_price_mail.ini")
SYSTEM_SENDERS = {
    "noreply@mail.iori0624.jp",
    "mailer-daemon@mail.iori0624.jp",
    "postmaster@mail.iori0624.jp",
}


def _load_config() -> dict:
    path = Path(os.environ.get("MFU_TICKET_PRICE_MAIL_CONFIG", DEFAULT_CONFIG_PATH))
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open("r", encoding="utf-8") as stream:
            parser.read_file(stream)
    except OSError as exc:
        raise RuntimeError(f"受信設定を読み込めません: {path}") from exc
    if not parser.has_section("imap"):
        raise RuntimeError(f"受信設定に[imap]セクションがありません: {path}")
    section = parser["imap"]
    password_path = Path(section.get("password_file", "").strip())
    if not password_path.is_absolute():
        password_path = path.parent / password_path
    try:
        if password_path.stat().st_mode & 0o007:
            raise RuntimeError("受信パスワードファイルを他ユーザーからアクセス不可にしてください")
        password = password_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("受信パスワードファイルを読み込めません") from exc
    if not password:
        raise RuntimeError("受信パスワードが空です")
    return {
        "host": section.get("host", "mail.iori0624.jp").strip(),
        "port": section.getint("port", 993),
        "username": section.get("username", "dt@mail.iori0624.jp").strip(),
        "password": password,
        "mailbox": section.get("mailbox", "INBOX").strip() or "INBOX",
        "max_messages": max(1, min(section.getint("max_messages", 20), 100)),
    }


def _sender_from_message(message) -> str:
    for header in ("Return-Path", "Sender", "From"):
        _display, address = parseaddr(str(message.get(header) or ""))
        normalized = (address or "").strip().lower()
        if normalized:
            return normalized
    return ""


def _from_header_sender(message) -> str:
    _display, address = parseaddr(str(message.get("From") or ""))
    return (address or "").strip().lower()


def _message_identifier(message, raw: bytes) -> str:
    raw_message_id = str(message.get("Message-ID") or "").strip()
    if raw_message_id:
        return raw_message_id[:500]
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _is_automatic_message(message, sender: str) -> bool:
    auto_submitted = str(message.get("Auto-Submitted") or "").strip().lower()
    precedence = str(message.get("Precedence") or "").strip().lower()
    return (
        sender in SYSTEM_SENDERS
        or (auto_submitted and auto_submitted != "no")
        or precedence in {"bulk", "junk", "list"}
    )


def _send_failure_notice(recipient: str) -> None:
    from app.utils.mail import send_mail

    send_mail(
        recipient,
        "【MFU】ディズニーチケット価格一覧を生成できませんでした",
        (
            "チケット価格一覧を取得できなかったため、PDFを生成できませんでした。\n"
            "時間を空けてから、dt@mail.iori0624.jp へ再度メールを送信してください。"
        ),
        from_display_name="MFU チケット価格情報",
        mail_kind="ticket_price_reply_failed",
        append_signature=False,
    )


def _process_message(raw: bytes, logger: logging.Logger) -> str:
    from app.ticket_price_research.mail_service import send_disney_ticket_price_reply
    from app.ticket_price_research.repository import (
        finish_request,
        get_recipient_by_email,
        mark_recipient_received,
        mark_recipient_sent,
        start_request,
    )

    message = BytesParser(policy=policy.default).parsebytes(raw)
    sender = _sender_from_message(message)
    message_id = _message_identifier(message, raw)
    request_id = start_request(message_id, sender)
    if request_id is None:
        return "duplicate"
    if _is_automatic_message(message, sender):
        finish_request(request_id, status="ignored_automatic")
        logger.info("TICKET_PRICE_MAIL ignored automatic sender=%s", sender)
        return "ignored_automatic"
    header_sender = _from_header_sender(message)
    if not sender or not header_sender or header_sender != sender:
        finish_request(
            request_id,
            status="rejected_sender_mismatch",
            error="Return-PathとFromが一致しません",
        )
        logger.warning(
            "TICKET_PRICE_MAIL rejected sender mismatch envelope=%s from=%s",
            sender,
            header_sender,
        )
        return "rejected_sender_mismatch"

    recipient = get_recipient_by_email(sender)
    if not recipient or not recipient.get("is_active"):
        finish_request(request_id, status="rejected_unknown")
        logger.warning("TICKET_PRICE_MAIL rejected unregistered sender=%s", sender)
        return "rejected_unknown"

    recipient_id = int(recipient["id"])
    if not recipient.get("rate_limit_ok"):
        mark_recipient_received(recipient_id, result="rate_limited")
        finish_request(request_id, status="rate_limited")
        logger.warning("TICKET_PRICE_MAIL rate limited sender=%s", sender)
        return "rate_limited"

    mark_recipient_received(recipient_id, result="processing")
    try:
        payload = send_disney_ticket_price_reply(str(recipient["email"]))
    except Exception as exc:
        error = str(exc)
        logger.exception("TICKET_PRICE_MAIL reply failed sender=%s", sender)
        try:
            _send_failure_notice(str(recipient["email"]))
        except Exception:
            logger.exception("TICKET_PRICE_MAIL failure notice failed sender=%s", sender)
        mark_recipient_received(recipient_id, result="failed", error=error)
        finish_request(request_id, status="failed", error=error)
        return "failed"

    mark_recipient_sent(recipient_id)
    finish_request(
        request_id,
        status="sent",
        item_count=int(payload.get("count") or 0),
        fetched_at=str(payload.get("fetched_at") or ""),
    )
    logger.info(
        "TICKET_PRICE_MAIL sent sender=%s count=%s",
        sender,
        payload.get("count"),
    )
    return "sent"


def run() -> dict:
    config = _load_config()
    logger = logging.getLogger("mfu.ticket_price_mail")
    result = {
        "checked": 0,
        "sent": 0,
        "duplicate": 0,
        "rejected_unknown": 0,
        "rejected_sender_mismatch": 0,
        "ignored_automatic": 0,
        "rate_limited": 0,
        "failed": 0,
    }
    client = imaplib.IMAP4_SSL(
        config["host"],
        config["port"],
        ssl_context=ssl.create_default_context(),
    )
    try:
        client.login(config["username"], config["password"])
        status, _data = client.select(config["mailbox"])
        if status != "OK":
            raise RuntimeError("受信トレイを開けません")
        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("未処理メールを検索できません")
        message_ids = (data[0] or b"").split()[: config["max_messages"]]
        if message_ids:
            # 未処理メールがある時だけMFU本体を読み込む。通常の空振り確認で
            # Flask全体を毎分起動せず、CPU・メモリ消費を抑える。
            from app import app

            logger = app.logger
            with app.app_context():
                for imap_id in message_ids:
                    status, parts = client.fetch(imap_id, "(RFC822)")
                    if status != "OK" or not parts or not isinstance(parts[0], tuple):
                        logger.warning("TICKET_PRICE_MAIL fetch failed imap_id=%s", imap_id)
                        continue
                    raw = bytes(parts[0][1])
                    outcome = _process_message(raw, logger)
                    result["checked"] += 1
                    result[outcome] = result.get(outcome, 0) + 1
                    client.store(imap_id, "+FLAGS", "\\Seen")
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return result


def main() -> int:
    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 2 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
