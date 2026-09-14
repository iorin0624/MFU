from __future__ import annotations

import hashlib
import html
import io
import re
from dataclasses import dataclass
from datetime import date
from email import policy
from email.message import Message
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from app.mail_filters.service import fetch_message, get_mailbox, list_mailboxes, preview_rule, search_message_id


MAX_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_ARTIFACTS_PER_MESSAGE = 10


class _PlainTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


@dataclass(frozen=True)
class EmailArtifact:
    filename: str
    content: bytes
    source_key: str


def mailbox_options() -> list[dict[str, Any]]:
    return list_mailboxes()


def folder_options(mailbox: str) -> list[str]:
    return [str(value) for value in (get_mailbox(mailbox).get("folders") or [])]


def search_messages(
    *, mailbox: str, folder: str, date_from: date, date_to: date,
    sender: str = "", subject: str = "",
) -> list[dict[str, Any]]:
    conditions: list[dict[str, Any]] = []
    if sender.strip():
        conditions.append({"type": "header", "operator": "contains", "fields": ["from"], "values": [sender.strip()]})
    if subject.strip():
        conditions.append({"type": "header", "operator": "contains", "fields": ["subject"], "values": [subject.strip()]})
    if not conditions:
        conditions.append({"type": "true", "operator": "is", "fields": [], "values": []})
    result = preview_rule(
        mailbox,
        {"name": "定期経費の証憑検索", "enabled": True, "mode": "all", "conditions": conditions, "actions": [{"type": "keep"}]},
        {
            "source_folder": folder,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "unread_only": False,
            "max_matches": 100,
        },
    )
    return list(result.get("items") or [])


def find_message_id(*, mailbox: str, message_id: str) -> list[dict[str, Any]]:
    return search_message_id(mailbox, message_id)


def load_message(mailbox: str, folder: str, uid: int) -> bytes:
    return fetch_message(mailbox, folder, uid)


def _message_text(message: Message) -> str:
    body = message.get_body(preferencelist=("plain", "html")) if hasattr(message, "get_body") else None
    if body is None:
        return ""
    try:
        content = str(body.get_content())
    except Exception:
        return ""
    if body.get_content_type() == "text/html":
        parser = _PlainTextHTMLParser()
        parser.feed(content)
        return parser.text()
    return content.strip()


def _safe_filename(value: str, fallback: str, suffix: str) -> str:
    name = Path(str(value or "")).name.replace("\x00", "").strip()
    stem = Path(name).stem if name else fallback
    stem = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", stem).strip(" ._") or fallback
    return f"{stem[:180]}{suffix}"


def _image_pdf(payload: bytes) -> bytes:
    with Image.open(io.BytesIO(payload)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")
        output = io.BytesIO()
        image.save(output, format="PDF", resolution=150.0)
        return output.getvalue()


def _body_pdf(message: Message) -> bytes:
    try:
        from weasyprint import HTML
    except Exception as exc:
        raise RuntimeError("メール本文のPDF変換機能を利用できません") from exc
    subject = str(message.get("subject") or "（件名なし）")
    sender = str(message.get("from") or "")
    recipient = str(message.get("to") or "")
    received = str(message.get("date") or "")
    body = _message_text(message) or "（本文なし）"
    document = f"""<!doctype html><html lang="ja"><meta charset="utf-8"><style>
      @page {{ size:A4; margin:18mm; }} body {{ font-family:sans-serif; font-size:11pt; color:#172033; }}
      h1 {{ font-size:16pt; overflow-wrap:anywhere; }} table {{ width:100%; border-collapse:collapse; margin-bottom:16px; }}
      th,td {{ border:1px solid #ccd5e2; padding:6px; text-align:left; vertical-align:top; overflow-wrap:anywhere; }}
      th {{ width:22%; background:#f4f7fb; }} pre {{ white-space:pre-wrap; overflow-wrap:anywhere; font-family:sans-serif; line-height:1.55; }}
    </style><h1>{html.escape(subject)}</h1><table>
      <tr><th>差出人</th><td>{html.escape(sender)}</td></tr><tr><th>宛先</th><td>{html.escape(recipient)}</td></tr>
      <tr><th>送信日時</th><td>{html.escape(received)}</td></tr></table><pre>{html.escape(body)}</pre></html>"""
    return HTML(string=document).write_pdf()


def extract_artifacts(raw: bytes, *, mailbox: str) -> tuple[dict[str, str], list[EmailArtifact]]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = str(message.get("subject") or "（件名なし）")
    message_id = str(message.get("message-id") or "").strip()
    identity = message_id or hashlib.sha256(raw).hexdigest()
    artifacts: list[EmailArtifact] = []
    for index, part in enumerate(message.iter_attachments()):
        if len(artifacts) >= MAX_ARTIFACTS_PER_MESSAGE:
            break
        payload = part.get_payload(decode=True) or b""
        if not payload or len(payload) > MAX_ARTIFACT_BYTES:
            continue
        content_type = str(part.get_content_type() or "").lower()
        filename = str(part.get_filename() or "")
        try:
            if content_type == "application/pdf" and payload.startswith(b"%PDF-"):
                content = payload
                output_name = _safe_filename(filename, "mail_attachment", ".pdf")
            elif content_type.startswith("image/"):
                content = _image_pdf(payload)
                output_name = _safe_filename(filename, "mail_image", ".pdf")
            else:
                continue
        except Exception:
            continue
        source_key = hashlib.sha256(f"{mailbox}\0{identity}\0part:{index}".encode("utf-8")).hexdigest()
        artifacts.append(EmailArtifact(output_name, content, source_key))
    if not artifacts:
        content = _body_pdf(message)
        source_key = hashlib.sha256(f"{mailbox}\0{identity}\0body".encode("utf-8")).hexdigest()
        artifacts.append(EmailArtifact(_safe_filename(subject, "mail", ".pdf"), content, source_key))
    return {
        "subject": subject,
        "from": str(message.get("from") or ""),
        "date": str(message.get("date") or ""),
    }, artifacts
