"""Safe card destinations shared by Media Hub notification views."""

from urllib.parse import urlparse


def notification_card_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 1000 or "\\" in raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return ""
    if raw.startswith("/") and not raw.startswith("//"):
        raw = f"https://mfu.iori0624.jp{raw}"
    try:
        parsed = urlparse(raw)
        parsed.port  # Reject malformed ports.
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    if parsed.hostname.lower() == "mfu.iori0624.jp" and parsed.path.rstrip("/") == "/mfu-notifications":
        return ""
    return raw
