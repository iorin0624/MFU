"""Keyboard-wedge decoding must preserve URL case and punctuation."""

import importlib.util
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "tools" / "mfu_qr_listener" / "scan_decoder.py"
spec = importlib.util.spec_from_file_location("scan_decoder", SOURCE)
assert spec and spec.loader
decoder_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(decoder_module)


def _type(decoder, text: str) -> str:
    """Generate US keyboard events, including Shift for case and URL symbols."""
    shifted = {
        ":": "KEY_SEMICOLON", "?": "KEY_SLASH", "#": "KEY_3",
    }
    simple = {
        "/": "KEY_SLASH", ".": "KEY_DOT", "-": "KEY_MINUS",
        "=": "KEY_EQUAL", "%": None,
    }
    for char in text:
        if char == "%":
            key, need_shift = "KEY_5", True
        elif char in shifted:
            key, need_shift = shifted[char], True
        elif char in simple:
            key, need_shift = simple[char], False
        elif char.isalpha():
            key, need_shift = "KEY_" + char.upper(), char.isupper()
        else:
            key, need_shift = "KEY_" + char, False
        if need_shift:
            decoder.feed("KEY_LEFTSHIFT", 1)
        decoder.feed(key, 1)
        decoder.feed(key, 0)
        if need_shift:
            decoder.feed("KEY_LEFTSHIFT", 0)
    return decoder.feed("KEY_ENTER", 1)


def test_url_punctuation_and_case_are_preserved():
    value = "https://www.webq.jp/orca/936537937/OnKkUz?token=a%2Fb#part"
    assert _type(decoder_module.ScanDecoder(), value) == value
    assert decoder_module.classify_code(value) == "URL"


def test_legacy_codes_and_alt_keypad_still_work():
    decoder = decoder_module.ScanDecoder()
    assert _type(decoder, "4901234567890") == "4901234567890"
    assert decoder_module.classify_code("4901234567890") == "JAN"
    assert _type(decoder, "30S") == "30S"
    assert decoder_module.classify_code("30S") == "SEC"
    decoder.feed("KEY_LEFTALT", 1)
    for digit in "0047":
        decoder.feed("KEY_KP" + digit, 1)
    decoder.feed("KEY_LEFTALT", 0)
    assert decoder.feed("KEY_ENTER", 1) == "/"


def test_only_http_urls_are_classified_as_urls():
    assert decoder_module.classify_code("www.example.com/path") == "URL"
    for value in ("javascript:alert(1)", "file:///etc/passwd", "https://user:pass@example.com", "https://a b"):
        assert decoder_module.classify_code(value) == "OTHER"


def test_japanese_scanner_apostrophe_is_corrected_only_in_url_scheme():
    raw = "https'//www.example.com/O'Brien?next=1"
    fixed = "https://www.example.com/O'Brien?next=1"
    assert decoder_module.normalize_scanned_url(raw) == fixed
    assert decoder_module.classify_code(raw) == "URL"
    assert decoder_module.normalize_scanned_url("Bob's barcode") == "Bob's barcode"
