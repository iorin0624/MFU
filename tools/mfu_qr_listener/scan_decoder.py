"""Decode keyboard-wedge scanner events without losing URL punctuation or case."""

from __future__ import annotations

from urllib.parse import urlsplit


JAN_LENGTHS = (8, 12, 13)

_PUNCTUATION = {
    "KEY_MINUS": ("-", "_"),
    "KEY_EQUAL": ("=", "+"),
    "KEY_LEFTBRACE": ("[", "{"),
    "KEY_RIGHTBRACE": ("]", "}"),
    "KEY_BACKSLASH": ("\\", "|"),
    "KEY_SEMICOLON": (";", ":"),
    "KEY_APOSTROPHE": ("'", '"'),
    "KEY_GRAVE": ("`", "~"),
    "KEY_COMMA": (",", "<"),
    "KEY_DOT": (".", ">"),
    "KEY_SLASH": ("/", "?"),
    "KEY_SPACE": (" ", " "),
}
_SHIFTED_DIGITS = ")!@#$%^&*("
_SHIFT_KEYS = {"KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"}
_ALT_KEYS = {"KEY_LEFTALT", "KEY_RIGHTALT"}
_ENTER_KEYS = {"KEY_ENTER", "KEY_KPENTER"}
_KEYPAD_PUNCTUATION = {
    "KEY_KPDOT": ".",
    "KEY_KPSLASH": "/",
    "KEY_KPMINUS": "-",
    "KEY_KPPLUS": "+",
    "KEY_KPASTERISK": "*",
}


def _character(keycode: str, shifted: bool) -> str:
    if keycode.startswith("KEY_KP") and keycode[6:].isdigit():
        return keycode[6:]
    if keycode in _KEYPAD_PUNCTUATION:
        return _KEYPAD_PUNCTUATION[keycode]
    if not keycode.startswith("KEY_"):
        return ""
    name = keycode[4:]
    if len(name) == 1 and "A" <= name <= "Z":
        return name if shifted else name.lower()
    if len(name) == 1 and name.isdigit():
        return _SHIFTED_DIGITS[int(name)] if shifted else name
    pair = _PUNCTUATION.get(keycode)
    return pair[int(shifted)] if pair else ""


class ScanDecoder:
    """Return a complete scan on Enter, otherwise None. value is evdev key state."""

    def __init__(self) -> None:
        self.buffer = ""
        self.shift_keys: set[str] = set()
        self.alt_mode = False
        self.alt_digits = ""

    def feed(self, keycode: str, value: int) -> str | None:
        if keycode in _SHIFT_KEYS:
            if value == 1:
                self.shift_keys.add(keycode)
            elif value == 0:
                self.shift_keys.discard(keycode)
            return None

        if keycode in _ALT_KEYS:
            if value == 1:
                self.alt_mode = True
                self.alt_digits = ""
            elif value == 0:
                if self.alt_mode and self.alt_digits:
                    try:
                        self.buffer += chr(int(self.alt_digits))
                    except (ValueError, OverflowError):
                        pass
                self.alt_mode = False
                self.alt_digits = ""
            return None

        if value != 1:
            return None
        if keycode in _ENTER_KEYS:
            result = self.buffer
            self.buffer = ""
            return result
        if self.alt_mode:
            if keycode.startswith("KEY_KP") and keycode[6:].isdigit():
                self.alt_digits += keycode[6:]
            return None

        self.buffer += _character(keycode, bool(self.shift_keys))
        return None


def is_http_url(value: str) -> bool:
    if any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        return False
    candidate = "https://" + value if value.lower().startswith("www.") else value
    try:
        parsed = urlsplit(candidate)
        parsed.port  # Reject malformed ports.
        return (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def classify_code(value: str) -> str:
    upper = value.upper()
    if is_http_url(value):
        return "URL"
    if upper == "START":
        return "START"
    if value.isdigit():
        return "JAN" if len(value) in JAN_LENGTHS else "MIN"
    if upper.endswith("S") and value[:-1].isdigit():
        return "SEC"
    return "OTHER"
