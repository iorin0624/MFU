"""Signed delivery from INPA to MFU's local notification endpoint."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
import time

from flask import current_app

_PATH = "/api/internal/inpa/feedback-notification"


def send_feedback_notification(payload: dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    nonce = secrets.token_urlsafe(24)
    sender = "inpa-feedback"
    digest = hashlib.sha256(body).hexdigest()
    canonical = f"POST\n{_PATH}\n{timestamp}\n{nonce}\n{digest}\n{sender}".encode()
    signature = hmac.new(
        current_app.config["INTERNAL_ADMIN_HMAC_SECRET"].encode(), canonical, hashlib.sha256
    ).hexdigest()
    connection = http.client.HTTPConnection(
        current_app.config["MFU_NOTIFICATION_HOST"],
        current_app.config["MFU_NOTIFICATION_PORT"],
        timeout=12,
    )
    try:
        connection.request("POST", _PATH, body=body, headers={
            "Content-Type": "application/json",
            "X-INPA-Timestamp": timestamp,
            "X-INPA-Nonce": nonce,
            "X-INPA-Admin": sender,
            "X-INPA-Signature": signature,
        })
        response = connection.getresponse()
        response.read()
        if response.status >= 400:
            raise RuntimeError(f"MFU notification endpoint returned HTTP {response.status}")
    finally:
        connection.close()
