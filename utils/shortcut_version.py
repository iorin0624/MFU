from __future__ import annotations

from typing import Any


def evaluate_shortcut_version(
    raw_version: Any,
    *,
    current_version: int,
    minimum_supported_version: int,
    allow_unversioned: bool,
) -> dict[str, Any]:
    """Evaluate a client version without depending on Flask or database state."""
    current = max(1, int(current_version or 1))
    minimum = max(0, min(int(minimum_supported_version or 0), current))
    text = "" if raw_version is None else str(raw_version).strip()
    if not text:
        if allow_unversioned:
            return {"ok": True, "client_version": 0, "legacy": True, "reason": "legacy_allowed"}
        return {"ok": False, "client_version": None, "legacy": True, "reason": "version_required"}
    if not text.isdigit():
        return {"ok": False, "client_version": None, "legacy": False, "reason": "invalid_version"}
    client = int(text)
    if client < minimum:
        return {"ok": False, "client_version": client, "legacy": client == 0, "reason": "version_too_old"}
    if client > current:
        return {"ok": False, "client_version": client, "legacy": False, "reason": "version_too_new"}
    return {"ok": True, "client_version": client, "legacy": client == 0, "reason": "supported"}
