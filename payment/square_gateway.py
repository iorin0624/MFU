"""Shared, retry-safe transport helpers for Square REST APIs.

This module deliberately has no Flask or database dependency so the payment
and invoice features can share identical versioning, retry, and error rules.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import random
import time
from typing import Any, Callable

import requests


DEFAULT_SQUARE_API_VERSION = "2025-08-20"
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class SquareApiErrorInfo:
    code: str
    detail: str
    category: str = ""


class SquareTransportError(RuntimeError):
    """Raised when Square did not return an HTTP response after safe retries."""

    def __init__(self, message: str, *, attempts: int, cause: Exception | None = None):
        super().__init__(message)
        self.attempts = attempts
        self.cause = cause


def square_api_version() -> str:
    return (os.environ.get("SQUARE_API_VERSION") or DEFAULT_SQUARE_API_VERSION).strip()


def square_headers(access_token: str, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Square-Version": square_api_version(),
    }
    if extra:
        headers.update(extra)
    return headers


def square_error_info(response: requests.Response) -> SquareApiErrorInfo:
    try:
        payload = response.json() if response.content else {}
    except Exception:
        payload = {}
    errors = payload.get("errors") or [] if isinstance(payload, dict) else []
    first = errors[0] if errors and isinstance(errors[0], dict) else {}
    return SquareApiErrorInfo(
        code=str(first.get("code") or f"HTTP_{response.status_code}"),
        detail=str(first.get("detail") or response.text or "Square API error"),
        category=str(first.get("category") or ""),
    )


def request_square(
    method: str,
    url: str,
    *,
    access_token: str,
    json_body: dict[str, Any] | None = None,
    timeout: int | float = 25,
    idempotency_key: str | None = None,
    retry_safe: bool = False,
    attempts: int = 3,
    transport: Callable[..., requests.Response] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> requests.Response:
    """Call Square and retry only operations proven safe to repeat.

    POST requests are retried only when the caller supplies the same
    idempotency key that is present in the unchanged JSON body. GET/HEAD and
    explicitly idempotent operations can opt in with ``retry_safe``.
    """

    verb = (method or "GET").upper()
    max_attempts = max(1, int(attempts or 1))
    can_retry = verb in {"GET", "HEAD"} or bool(idempotency_key) or retry_safe
    if verb == "POST" and idempotency_key:
        body_key = (json_body or {}).get("idempotency_key")
        if body_key != idempotency_key:
            raise ValueError("idempotency_key must match the unchanged Square request body")
    request_fn = transport or requests.request
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            response = request_fn(
                verb,
                url,
                headers=square_headers(access_token),
                json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_error = exc
            if not can_retry or attempt >= max_attempts:
                raise SquareTransportError(
                    "Square APIへの接続結果を確認できませんでした。",
                    attempts=attempt,
                    cause=exc,
                ) from exc
        else:
            request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
            logging.info(
                "square api response method=%s status=%s attempt=%s request_id=%s version=%s",
                verb,
                response.status_code,
                attempt,
                request_id or "-",
                response.headers.get("Square-Version") or square_api_version(),
            )
            if response.status_code not in TRANSIENT_HTTP_STATUSES or not can_retry or attempt >= max_attempts:
                return response

        delay = min(2.0, 0.2 * (2 ** (attempt - 1))) + random.uniform(0, 0.15)
        sleeper(delay)

    raise SquareTransportError(
        "Square APIへの接続結果を確認できませんでした。",
        attempts=max_attempts,
        cause=last_error,
    )
