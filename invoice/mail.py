from __future__ import annotations

from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

from app.utils.mail import send_mime

from .services import log_mail_result, mark_invoice_mailed
from .utils import now_jst, split_emails


def send_invoice_mail(invoice: dict, *, to_email: str, cc_email: str | None, bcc_email: str | None, subject: str, body: str, attachment_filename: str, pdf_bytes: bytes) -> None:
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = formataddr((str(Header(invoice.get("issuer_name") or "MFU", "utf-8")), "noreply@mail.iori0624.jp"))
    msg["To"] = ", ".join(split_emails(to_email))
    if cc_email:
        msg["Cc"] = ", ".join(split_emails(cc_email))
    if bcc_email:
        msg["Bcc"] = ", ".join(split_emails(bcc_email))
    msg["Reply-To"] = invoice.get("contact_email_snapshot") or "admin@mail.iori0624.jp"
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
