from __future__ import annotations

import re


PUBLIC_TRAFFIC_SOURCE_HOSTS = frozenset({
    "pro.iori0624.jp",
    "suc.iori0624.jp",
})
PUBLIC_TRAFFIC_SOURCE_SESSION_KEY = "mfu_public_traffic_source"
PUBLIC_TRAFFIC_SOURCE_MAX_LENGTH = 80

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")


def public_traffic_source_host(host: str | None) -> bool:
    hostname = (host or "").partition(":")[0].rstrip(".").lower()
    return hostname in PUBLIC_TRAFFIC_SOURCE_HOSTS


def normalize_public_traffic_source(value: object | None) -> str | None:
    if value is None:
        return None
    cleaned = _CONTROL_CHARACTERS.sub(" ", str(value))
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    if not cleaned:
        return None
    return cleaned[:PUBLIC_TRAFFIC_SOURCE_MAX_LENGTH]
