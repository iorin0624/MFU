"""Encrypted DB-backed mail queue used by authentication flows."""

from __future__ import annotations

import hashlib
import json

from cryptography.fernet import Fernet
from flask import current_app
from sqlalchemy import text

from .auth.service import hash_secret, new_public_id
from .db import get_engine


def _cipher() -> Fernet:
    key = current_app.config["MAIL_ENCRYPTION_KEY"]
    if not key:
        raise RuntimeError("INPA_MAIL_ENCRYPTION_KEY is required to queue mail")
    return Fernet(key.encode("ascii"))


def queue_registration_email(recipient: str, registration_token: str) -> None:
    payload = {
        "registration_url": f"{current_app.config['PUBLIC_ORIGIN'].rstrip('/')}/register/complete?token={registration_token}",
    }
    cipher = _cipher()
    recipient_ciphertext = cipher.encrypt(recipient.encode("utf-8"))
    template_ciphertext = cipher.encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    idempotency_key = hashlib.sha256(f"registration:{hash_secret(registration_token)}".encode()).hexdigest()
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mail_logs "
                "(public_id, mail_type, recipient_hash, recipient_ciphertext, template_data_ciphertext, idempotency_key) "
                "VALUES (:public_id, 'registration_confirmation', :recipient_hash, :recipient, :template, :idempotency)"
            ),
            {
                "public_id": new_public_id(),
                "recipient_hash": hash_secret(recipient.casefold()),
                "recipient": recipient_ciphertext,
                "template": template_ciphertext,
                "idempotency": idempotency_key,
            },
        )


def decrypt_mail(recipient_ciphertext: bytes, template_ciphertext: bytes) -> tuple[str, dict[str, str]]:
    cipher = _cipher()
    recipient = cipher.decrypt(recipient_ciphertext).decode("utf-8")
    template = json.loads(cipher.decrypt(template_ciphertext).decode("utf-8"))
    return recipient, template
