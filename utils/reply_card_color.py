"""Color validation for the public processing-return card."""

import re


DEFAULT_REPLY_CARD_COLOR = "#f4b3c2"
_COLOR_RE = re.compile(r"#[0-9a-fA-F]{6}\Z")


def normalize_reply_card_color(value: object) -> str:
    """Allow only six-digit CSS hex colors in the public viewer."""
    color = str(value or "").strip()
    return color.lower() if _COLOR_RE.fullmatch(color) else DEFAULT_REPLY_CARD_COLOR
