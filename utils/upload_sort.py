from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


def natural_filename_sort_key(value: str) -> tuple:
    """Return a stable, case-insensitive key with numeric filename ordering."""
    parts = re.split(r"(\d+)", str(value or ""))
    return tuple(
        (0, int(part), len(part)) if part.isdigit() else (1, part.casefold())
        for part in parts
        if part
    )


def sort_upload_file_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            natural_filename_sort_key(str(row.get("filename") or "")),
            int(row.get("id") or 0),
        ),
    )
