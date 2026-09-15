from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


_CAPTURE_CACHE = ".capture-times.json"
_CAPTURE_TAGS = (
    "ExifIFD:DateTimeOriginal",
    "XMP-exif:DateTimeOriginal",
    "XMP-photoshop:DateCreated",
    "ExifIFD:CreateDate",
    "QuickTime:CreationDate",
    "QuickTime:CreateDate",
    "QuickTime:MediaCreateDate",
    "QuickTime:TrackCreateDate",
)


def natural_filename_sort_key(value: str) -> tuple:
    """Return a stable, case-insensitive key with numeric filename ordering."""
    parts = re.split(r"(\d+)", str(value or ""))
    return tuple(
        (0, int(part), len(part)) if part.isdigit() else (1, part.casefold())
        for part in parts
        if part
    )


def _normalized_capture_time(value: Any) -> str | None:
    match = re.match(
        r"^(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?",
        str(value or "").strip(),
    )
    if not match:
        return None
    fraction = (match.group(7) or "")[:6].ljust(6, "0")
    return (
        f"{match.group(1)}-{match.group(2)}-{match.group(3)}T"
        f"{match.group(4)}:{match.group(5)}:{match.group(6)}.{fraction}"
    )


def _read_capture_cache(original_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads((original_dir / _CAPTURE_CACHE).read_text(encoding="utf-8"))
        if int(payload.get("version") or 0) == 1 and isinstance(payload.get("files"), dict):
            return payload["files"]
    except (OSError, ValueError, TypeError):
        pass
    return {}


def _write_capture_cache(original_dir: Path, entries: dict[str, dict[str, Any]]) -> None:
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=original_dir,
            prefix=f"{_CAPTURE_CACHE}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump({"version": 1, "files": entries}, temporary, ensure_ascii=False, separators=(",", ":"))
        os.replace(temporary_name, original_dir / _CAPTURE_CACHE)
    except OSError:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _extract_capture_times(paths: list[Path]) -> dict[str, str | None]:
    if not paths:
        return {}
    command = [
        "exiftool", "-json", "-G1",
        "-DateTimeOriginal", "-CreateDate", "-DateCreated",
        "-CreationDate", "-MediaCreateDate", "-TrackCreateDate",
        *[str(path) for path in paths],
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=45, check=True)
        records = json.loads(completed.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return {path.name: None for path in paths}
    result: dict[str, str | None] = {}
    for record in records:
        source = Path(str(record.get("SourceFile") or ""))
        captured_at = None
        for tag in _CAPTURE_TAGS:
            captured_at = _normalized_capture_time(record.get(tag))
            if captured_at:
                break
        result[source.name] = captured_at
    return result


def capture_times_for_files(original_dir: Path, filenames: Iterable[str]) -> dict[str, str | None]:
    original_dir = Path(original_dir)
    cache = _read_capture_cache(original_dir)
    result: dict[str, str | None] = {}
    missing: list[Path] = []
    signatures: dict[str, tuple[int, int]] = {}
    for raw_name in filenames:
        filename = str(raw_name or "")
        if not filename or Path(filename).name != filename:
            continue
        path = original_dir / filename
        try:
            stat = path.stat()
        except OSError:
            continue
        signature = (int(stat.st_size), int(stat.st_mtime_ns))
        signatures[filename] = signature
        cached = cache.get(filename) or {}
        if (int(cached.get("size") or -1), int(cached.get("mtime_ns") or -1)) == signature:
            result[filename] = cached.get("captured_at") or None
        else:
            missing.append(path)

    extracted = _extract_capture_times(missing)
    result.update(extracted)
    if missing:
        for path in missing:
            size, mtime_ns = signatures[path.name]
            cache[path.name] = {
                "size": size,
                "mtime_ns": mtime_ns,
                "captured_at": extracted.get(path.name),
            }
        _write_capture_cache(original_dir, cache)
    return result


def sort_upload_file_rows(
    rows: Iterable[dict[str, Any]],
    *,
    original_dir: Path | None = None,
    capture_times: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    prepared = [dict(row) for row in rows]
    if capture_times is None and original_dir is not None:
        capture_times = capture_times_for_files(original_dir, (str(row.get("filename") or "") for row in prepared))
    capture_times = capture_times or {}
    for row in prepared:
        row["captured_at"] = capture_times.get(str(row.get("filename") or ""))
    return sorted(
        prepared,
        key=lambda row: (
            0 if row.get("captured_at") else 1,
            str(row.get("captured_at") or ""),
            natural_filename_sort_key(str(row.get("filename") or "")),
            int(row.get("id") or 0),
        ),
    )
