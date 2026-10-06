from __future__ import annotations

from typing import Any

import requests

from .repository import get_discord_webhook, record_discord_delivery


def post_discord_notification(
    feature_key: str,
    payload: dict[str, Any],
    *,
    legacy_webhook: str | None = None,
    timeout: int = 10,
    params: dict[str, Any] | None = None,
    mirror: bool = True,
) -> bool:
    if mirror:
        try:
            from app.utils.notification_service import mirror_discord_payload

            mirror_discord_payload(feature_key, payload)
        except Exception:
            # Discord delivery must remain available during staged migration.
            pass
    webhook = get_discord_webhook(feature_key, legacy_webhook)
    if not webhook:
        return False
    try:
        response = requests.post(webhook, json=payload, timeout=timeout, params=params)
        response.raise_for_status()
        record_discord_delivery(feature_key, success=True)
        return True
    except Exception as exc:
        record_discord_delivery(feature_key, success=False, error=f"{type(exc).__name__}: {exc}")
        raise
