"""Mail worker entrypoint reserved for the DB-backed mail queue phase."""

from __future__ import annotations

import logging
import smtplib
import time
from email.message import EmailMessage

from flask import current_app
from sqlalchemy import text

from . import create_app
from .db import get_engine
from .mail_queue import decrypt_mail

logger = logging.getLogger(__name__)


def _deliver_one() -> bool:
    with get_engine().begin() as connection:
        row = connection.execute(
            text(
                "SELECT id, recipient_ciphertext, template_data_ciphertext FROM mail_logs "
                "WHERE status IN ('queued', 'retry_wait') AND (next_attempt_at IS NULL OR next_attempt_at <= UTC_TIMESTAMP(6)) "
                "ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
        ).mappings().first()
        if not row:
            return False
        connection.execute(text("UPDATE mail_logs SET status = 'sending', attempt_count = attempt_count + 1 WHERE id = :id"), {"id": row["id"]})
    try:
        recipient, template = decrypt_mail(row["recipient_ciphertext"], row["template_data_ciphertext"])
        message = EmailMessage()
        message["From"] = current_app.config["MAIL_FROM"]
        message["To"] = recipient
        message["Subject"] = "INPA メールアドレスの確認"
        message.set_content(f"次のリンクから登録を完了してください。\n\n{template['registration_url']}\n")
        with smtplib.SMTP(current_app.config["SMTP_HOST"], current_app.config["SMTP_PORT"], timeout=15) as client:
            client.starttls()
            if current_app.config["SMTP_USERNAME"]:
                client.login(current_app.config["SMTP_USERNAME"], current_app.config["SMTP_PASSWORD"])
            client.send_message(message)
        with get_engine().begin() as connection:
            connection.execute(text("UPDATE mail_logs SET status = 'sent', sent_at = UTC_TIMESTAMP(6), recipient_ciphertext = NULL, template_data_ciphertext = NULL WHERE id = :id"), {"id": row["id"]})
    except Exception as exc:
        logger.exception("mail delivery failed")
        with get_engine().begin() as connection:
            connection.execute(text("UPDATE mail_logs SET status = 'retry_wait', next_attempt_at = DATE_ADD(UTC_TIMESTAMP(6), INTERVAL 5 MINUTE), last_error_code = :error WHERE id = :id"), {"error": type(exc).__name__[:64], "id": row["id"]})
    return True


def main() -> None:
    app = create_app("public")
    logging.basicConfig(level=logging.INFO)
    app.logger.info("INPA mail worker started")
    while True:
        with app.app_context():
            delivered = _deliver_one()
        time.sleep(1 if delivered else 15)


if __name__ == "__main__":
    main()
