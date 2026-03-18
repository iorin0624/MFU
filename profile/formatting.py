from __future__ import annotations

import html
import re

_URL_PATTERN = re.compile(r'(https?://[^\s<>"\')\]]+)')


def normalize_plain_text_for_display(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value
    normalized = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", normalized)
    normalized = re.sub(r"(?i)&lt;\s*br\s*/?\s*&gt;", "\n", normalized)
    normalized = re.sub(r"(?i)&amp;lt;\s*br\s*/?\s*&amp;gt;", "\n", normalized)

    return normalized.replace("\r\n", "\n").replace("\r", "\n")


def linkify_plain_text_for_display(value: str | None) -> str | None:
    normalized = normalize_plain_text_for_display(value)
    if normalized is None:
        return None

    escaped = html.escape(normalized)

    def repl(match: re.Match[str]) -> str:
        url = match.group(1)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer nofollow">{url}</a>'

    return _URL_PATTERN.sub(repl, escaped)
