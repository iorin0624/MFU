"""Helpers for the authenticated MFU upload speed test."""

from __future__ import annotations

from typing import BinaryIO


MEBIBYTE = 1024 * 1024
SPEEDTEST_UPLOAD_SIZES_MB = (3, 5, 10, 15)
SPEEDTEST_UPLOAD_SIZES_BYTES = frozenset(size * MEBIBYTE for size in SPEEDTEST_UPLOAD_SIZES_MB)
SPEEDTEST_READ_CHUNK_BYTES = 256 * 1024


class SpeedtestPayloadError(ValueError):
    """Raised when a speed-test payload is missing, malformed, or too large."""


def parse_expected_bytes(value: str | None) -> int:
    """Validate the browser-declared payload size against the approved photo sizes."""
    if not value:
        raise SpeedtestPayloadError("送信サイズを確認できませんでした。")
    try:
        expected_bytes = int(value)
    except (TypeError, ValueError) as exc:
        raise SpeedtestPayloadError("送信サイズが不正です。") from exc
    if expected_bytes not in SPEEDTEST_UPLOAD_SIZES_BYTES:
        raise SpeedtestPayloadError("許可されていない送信サイズです。")
    return expected_bytes


def validate_content_length(content_length: int | None, expected_bytes: int) -> None:
    """Reject a known HTTP body length that differs from the declared size."""
    if content_length is not None and content_length != expected_bytes:
        raise SpeedtestPayloadError("送信データのサイズが一致しません。")


def consume_upload(stream: BinaryIO, expected_bytes: int) -> int:
    """Read and count the body without retaining the uploaded test data."""
    received_bytes = 0
    while True:
        chunk = stream.read(SPEEDTEST_READ_CHUNK_BYTES)
        if not chunk:
            break
        received_bytes += len(chunk)
        if received_bytes > expected_bytes:
            raise SpeedtestPayloadError("送信データが指定サイズを超えています。")
    if received_bytes != expected_bytes:
        raise SpeedtestPayloadError("送信データのサイズが一致しません。")
    return received_bytes
