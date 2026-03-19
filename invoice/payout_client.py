from __future__ import annotations

import os
from typing import Any

import requests
from flask import current_app

from .services import build_invoice_payout_memo

DEFAULT_TIMEOUT_SECONDS = 10


class InvoicePayoutClientError(RuntimeError):
    pass


def _config_value(name: str) -> str:
    value = current_app.config.get(name) or os.environ.get(name) or ""
    return str(value).strip()


def create_invoice_payout_access(invoice: dict[str, Any]) -> dict[str, Any]:
    api_url = _config_value("PAYOUT_TOKEN_API_URL")
    api_key = _config_value("PAYOUT_TOKEN_API_KEY")
    if not api_url:
        raise InvoicePayoutClientError("PAYOUT_TOKEN_API_URL が未設定です。")
    if not api_key:
        raise InvoicePayoutClientError("PAYOUT_TOKEN_API_KEY が未設定です。")

    payload = {
        "memo": build_invoice_payout_memo(invoice),
        "issued_by_app": "invoice",
    }
    headers = {
        "Content-Type": "application/json",
        "X-MFU-PAYOUT-API-KEY": api_key,
    }
    try:
        response = requests.post(
            api_url,
            json=payload,
            headers=headers,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise InvoicePayoutClientError(f"payout API への接続に失敗しました: {exc}") from exc

    try:
        response_data = response.json()
    except ValueError as exc:
        raise InvoicePayoutClientError(
            f"payout API の応答が JSON ではありませんでした: status={response.status_code}"
        ) from exc

    if response.status_code >= 400 or not response_data.get("ok"):
        error_message = response_data.get("error") or response.text or "unknown_error"
        raise InvoicePayoutClientError(
            f"payout API が失敗しました: status={response.status_code} error={error_message}"
        )

    access_url = str(response_data.get("access_url") or "").strip()
    token = str(response_data.get("token") or "").strip()
    if not access_url and token:
        public_base_url = (
            current_app.config.get("PAYOUT_PUBLIC_BASE_URL")
            or os.environ.get("PAYOUT_PUBLIC_BASE_URL")
            or "https://mfu.iori0624.jp"
        )
        access_url = f"{str(public_base_url).rstrip('/')}/payout?iv={token}"
        response_data["access_url"] = access_url
    if not access_url:
        raise InvoicePayoutClientError("payout API の応答に access_url がありません。")
    return response_data
