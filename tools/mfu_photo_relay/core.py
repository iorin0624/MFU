from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


APP_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "MFU" / "MFU Photo Relay"
SETTINGS_PATH = APP_DIR / "settings.json"
HISTORY_PATH = APP_DIR / "history.json"

NAMING_ORIGINAL = "original"
NAMING_DATETIME = "datetime"
NAMING_SEQUENCE = "sequence"
VALID_NAMING_MODES = {NAMING_ORIGINAL, NAMING_DATETIME, NAMING_SEQUENCE}

WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def default_target_folder() -> str:
    return str(Path.home() / "Pictures" / "MFU受信")


def default_settings() -> dict[str, Any]:
    return {
        "base_url": os.environ.get("MFU_BASE_URL", "https://mfu.iori0624.jp").rstrip("/"),
        "device_uuid": str(uuid.uuid4()),
        "device_name": os.environ.get("COMPUTERNAME") or "Windows PC",
        "target_folder": default_target_folder(),
        "naming_mode": NAMING_ORIGINAL,
        "sequence_digits": 5,
        "next_sequence": 1,
        "auto_start": False,
    }


def load_settings() -> dict[str, Any]:
    values = default_settings()
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            values.update(saved)
    except (OSError, ValueError):
        pass
    if values.get("naming_mode") not in VALID_NAMING_MODES:
        values["naming_mode"] = NAMING_ORIGINAL
    try:
        values["device_uuid"] = str(uuid.UUID(str(values.get("device_uuid"))))
    except (ValueError, TypeError, AttributeError):
        values["device_uuid"] = str(uuid.uuid4())
    values["sequence_digits"] = min(12, max(1, int(values.get("sequence_digits") or 5)))
    values["next_sequence"] = max(0, int(values.get("next_sequence") or 1))
    return values


def save_settings(values: dict[str, Any]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)


def load_history() -> dict[str, dict[str, str]]:
    try:
        result = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def save_history(history: dict[str, dict[str, str]]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    # Old acknowledgements are only needed to make a retried ACK idempotent.
    if len(history) > 5000:
        history = dict(list(history.items())[-5000:])
    temporary = HISTORY_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, HISTORY_PATH)


def sanitize_windows_filename(value: str, fallback: str = "photo.jpg") -> str:
    name = Path(str(value or "")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = fallback
    stem, suffix = os.path.splitext(name)
    if stem.upper() in WINDOWS_RESERVED:
        stem = f"_{stem}"
    return (stem[:220] + suffix[:20]) or fallback


def _extension(payload: dict[str, Any]) -> str:
    if payload.get("converted_to_jpeg") or payload.get("mime_type") == "image/jpeg":
        return ".jpg"
    if payload.get("mime_type") == "image/png":
        return ".png"
    suffix = Path(str(payload.get("original_filename") or "")).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png"} else ".jpg"


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    for parser in (
        lambda: datetime.fromisoformat(normalized),
        lambda: datetime.strptime(text, "%Y:%m:%d %H:%M:%S"),
        lambda: datetime.strptime(text, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            parsed = parser()
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
        except ValueError:
            continue
    return None


def unique_path(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    for index in range(2, 100_000):
        alternate = folder / f"{stem}_{index:02d}{suffix}"
        if not alternate.exists():
            return alternate
    raise RuntimeError("保存ファイル名を確保できません。")


@dataclass(frozen=True)
class NameResult:
    path: Path
    next_sequence: int


def choose_output_path(
    folder: Path,
    payload: dict[str, Any],
    *,
    naming_mode: str,
    sequence_digits: int,
    next_sequence: int,
    now: datetime | None = None,
) -> NameResult:
    folder.mkdir(parents=True, exist_ok=True)
    extension = _extension(payload)
    if naming_mode == NAMING_SEQUENCE:
        digits = min(12, max(1, int(sequence_digits)))
        number = max(0, int(next_sequence))
        while True:
            candidate = folder / f"{number:0{digits}d}{extension}"
            if not candidate.exists():
                return NameResult(candidate, number + 1)
            number += 1
    if naming_mode == NAMING_DATETIME:
        captured = (
            _parse_datetime(payload.get("capture_at"))
            or _parse_datetime(payload.get("source_modified_at"))
            or now
            or datetime.now()
        )
        return NameResult(unique_path(folder, captured.strftime("%Y-%m-%d_%H%M%S") + extension), next_sequence)
    original = sanitize_windows_filename(str(payload.get("original_filename") or "photo"))
    if payload.get("converted_to_jpeg"):
        original = str(Path(original).with_suffix(".jpg"))
    return NameResult(unique_path(folder, original), next_sequence)

