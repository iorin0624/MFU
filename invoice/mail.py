from __future__ import annotations

from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

from app.utils.mail import send_mime

from .services import log_mail_result, mark_invoice_mailed
from .utils import now_jst, split_emails


def send_invoice_mail(
    invoice: dict,
    *,
    to_email: str,
    cc_email: str | None,
    bcc_email: str | None,
    reply_to_email: str | None,
    subject: str,
    body: str,
    attachment_filename: str,
    pdf_bytes: bytes,
) -> None:
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = formataddr((str(Header(invoice.get("issuer_name") or "MFU", "utf-8")), "noreply@mail.iori0624.jp"))
    msg["To"] = ", ".join(split_emails(to_email))
    if cc_email:
        msg["Cc"] = ", ".join(split_emails(cc_email))
    if bcc_email:
        msg["Bcc"] = ", ".join(split_emails(bcc_email))
    if reply_to_email:
        msg["Reply-To"] = reply_to_email.strip()
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    part = MIMEApplication(pdf_bytes, _subtype="pdf")
    part.add_header("Content-Disposition", "attachment", filename=str(Header(attachment_filename, "utf-8")))
    msg.attach(part)

    try:
        send_mime(msg)
        sent_at = now_jst()
        mark_invoice_mailed(int(invoice["id"]))
        log_mail_result(
            int(invoice["id"]),
            to_email=to_email,
            cc_email=cc_email,
            bcc_email=bcc_email,
            subject=subject,
            body=body,
            attachment_filename=attachment_filename,
            status="sent",
            sent_at=sent_at,
        )
    except Exception as exc:
        log_mail_result(
            int(invoice["id"]),
            to_email=to_email,
            cc_email=cc_email,
            bcc_email=bcc_email,
            subject=subject,
            body=body,
            attachment_filename=attachment_filename,
            status="failed",
            error_message=str(exc),
        )
        raise


def send_invoice_receipt_mail(
    invoice: dict,
    *,
    to_email: str,
    cc_email: str | None,
    bcc_email: str | None,
    reply_to_email: str | None,
    attachment_filename: str,
    pdf_bytes: bytes,
) -> None:
    invoice_no = (invoice.get("invoice_no") or "").strip()
    subject_text = (invoice.get("subject") or "").strip()
    subject = f"【領収書】{invoice_no} {subject_text}".strip()
    body = (
        f"{invoice.get('contact_name_snapshot') or ''} 様\n\n"
        "ご入金ありがとうございました。\n"
        "領収書PDFを添付いたします。\n\n"
        f"請求書番号: {invoice_no}\n"
        f"件名: {subject_text}\n"
    )

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = formataddr((str(Header(invoice.get("issuer_name") or "MFU", "utf-8")), "noreply@mail.iori0624.jp"))
    msg["To"] = ", ".join(split_emails(to_email))
    if cc_email:
        msg["Cc"] = ", ".join(split_emails(cc_email))
    if bcc_email:
        msg["Bcc"] = ", ".join(split_emails(bcc_email))
    if reply_to_email:
        msg["Reply-To"] = reply_to_email.strip()
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    part = MIMEApplication(pdf_bytes, _subtype="pdf")
    part.add_header("Content-Disposition", "attachment", filename=str(Header(attachment_filename, "utf-8")))
    msg.attach(part)

    try:
        send_mime(msg)
        log_mail_result(
            int(invoice["id"]),
            to_email=to_email,
            cc_email=cc_email,
            bcc_email=bcc_email,
            subject=subject,
            body=body,
            attachment_filename=attachment_filename,
            status="receipt_sent",
            sent_at=now_jst(),
        )
    except Exception as exc:
        log_mail_result(
            int(invoice["id"]),
            to_email=to_email,
            cc_email=cc_email,
            bcc_email=bcc_email,
            subject=subject,
            body=body,
            attachment_filename=attachment_filename,
            status="receipt_failed",
            error_message=str(exc),
        )
        raise
