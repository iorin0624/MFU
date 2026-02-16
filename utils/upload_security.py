"""アップロード時のセキュリティ検証ユーティリティ。"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable


# 必要に応じて拡張して利用する。
DEFAULT_ALLOWED_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".pdf": "application/pdf",
}

# 代表的な「危険な実行系拡張子」
DENY_EXTENSION_SEGMENTS = {
    "php",
    "phtml",
    "php3",
    "php4",
    "php5",
    "phar",
    "cgi",
    "pl",
    "py",
    "rb",
    "sh",
    "bash",
    "exe",
    "dll",
    "so",
    "js",
    "jsp",
    "asp",
    "aspx",
}


def sanitize_filename(name: str, used_names: set[str]) -> str:
    """ファイル名を安全化し、重複時は suffix を付与してユニーク化する。"""
    cleaned = re.sub(r"[\\/:*?\"<>|]", "_", name or "")
    cleaned = os.path.basename(cleaned).strip() or "unnamed"
    root, ext = os.path.splitext(cleaned)
    ext = ext.lower()
    candidate = f"{root}{ext}"

    seq = 2
    while candidate in used_names:
        candidate = f"{root}_{seq}{ext}"
        seq += 1

    used_names.add(candidate)
    return candidate


def has_double_extension(filename: str, denied_segments: Iterable[str] | None = None) -> bool:
    """shell.php.jpg のような二重拡張子を検出する。"""
    segments = [seg.lower() for seg in Path(filename).name.split(".") if seg]
    if len(segments) <= 2:
        return False

    denied = set(denied_segments or DENY_EXTENSION_SEGMENTS)
    return any(seg in denied for seg in segments[1:-1])


def detect_mime_from_bytes(head: bytes) -> str:
    """先頭バイトから簡易 MIME 判定する。"""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"%PDF"):
        return "application/pdf"
    return "application/octet-stream"


def validate_upload_file(
    *,
    filename: str,
    header_mime: str,
    detected_mime: str,
    allowed_extensions: dict[str, str],
) -> tuple[bool, str]:
    """拡張子・二重拡張子・MIME整合をチェックする。"""
    lowered = (filename or "").lower()
    ext = os.path.splitext(lowered)[1]

    if not ext or ext not in allowed_extensions:
        return False, f"許可されていない拡張子です: {ext or '(なし)'}"

    if has_double_extension(lowered):
        return False, "二重拡張子ファイルは拒否されました"

    expected_mime = allowed_extensions[ext]
    normalized_header = (header_mime or "").split(";")[0].strip().lower()

    # Header は参考程度。実データ判定を最優先する。
    if detected_mime != expected_mime:
        return False, (
            "MIME 不一致: "
            f"expected={expected_mime}, detected={detected_mime}, header={normalized_header or 'N/A'}"
        )

    return True, "ok"

