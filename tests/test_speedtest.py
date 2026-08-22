from io import BytesIO
from pathlib import Path

import pytest

from app.utils.speedtest import (
    MEBIBYTE,
    SPEEDTEST_UPLOAD_SIZES_BYTES,
    SpeedtestPayloadError,
    consume_upload,
    parse_expected_bytes,
    validate_content_length,
)


ROOT = Path(__file__).resolve().parents[1]


def test_speedtest_accepts_only_agreed_photo_sizes():
    assert {size // MEBIBYTE for size in SPEEDTEST_UPLOAD_SIZES_BYTES} == {3, 5, 10, 15}
    assert parse_expected_bytes(str(10 * MEBIBYTE)) == 10 * MEBIBYTE

    for invalid in (None, "", "abc", "0", str(16 * MEBIBYTE)):
        with pytest.raises(SpeedtestPayloadError):
            parse_expected_bytes(invalid)


def test_speedtest_validates_content_length_and_received_bytes():
    expected = 3 * MEBIBYTE
    validate_content_length(expected, expected)
    validate_content_length(None, expected)
    assert consume_upload(BytesIO(b"x" * expected), expected) == expected

    with pytest.raises(SpeedtestPayloadError):
        validate_content_length(expected - 1, expected)
    with pytest.raises(SpeedtestPayloadError):
        consume_upload(BytesIO(b"x" * (expected - 1)), expected)
    with pytest.raises(SpeedtestPayloadError):
        consume_upload(BytesIO(b"x" * (expected + 1)), expected)


def test_speedtest_template_uses_three_rounds_and_checks_http_results():
    template = (ROOT / "templates" / "speedtest.html").read_text(encoding="utf-8")
    assert "const TEST_SIZES_MB = [3, 5, 10, 15]" in template
    assert "const TEST_ROUNDS = 3" in template
    assert "response.ok" in template
    assert "/api/speedtest/upload" in template
    assert "/api/speedtest/ping" in template
