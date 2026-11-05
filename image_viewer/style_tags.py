"""Human-reviewable, hierarchical clothing style tags for image viewer files.

The file checksum is the stable key, so renaming or moving an image does not
detach its classification. No image contents or personal data enter this DB.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

MAJOR_STYLES = (
    {"id": "lolita", "label": "ロリータ系"},
    {"id": "jirai", "label": "地雷系"},
    {"id": "casual", "label": "私服系"},
)
MAJOR_IDS = {style["id"] for style in MAJOR_STYLES}
DEFAULT_DB_PATH = Path(os.environ.get("IMAGE_VIEWER_STYLE_DB", "/mnt/mfu/data/image_viewer_style_tags.sqlite3"))


def _tag(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("タグは文字列で指定してください。")
    label = " ".join(value.strip().split())
    if not label or len(label) > 50 or any(ord(char) < 32 for char in label):
        raise ValueError("タグは1～50文字で指定してください。")
    return label


def normalize_assignment(value: object) -> dict:
    """Require one major style and unique middle/small tags per parent."""
    if not isinstance(value, dict):
        raise ValueError("分類データの形式が正しくありません。")
    major = value.get("major")
    if major not in MAJOR_IDS:
        raise ValueError("大分類はロリータ系・地雷系・私服系から選択してください。")
    middle_values = value.get("middleTags", [])
    if not isinstance(middle_values, list) or len(middle_values) > 12:
        raise ValueError("中分類は12個以内にしてください。")
    middle_tags = []
    seen_middle: set[str] = set()
    for middle in middle_values:
        if not isinstance(middle, dict):
            raise ValueError("中分類の形式が正しくありません。")
        name = _tag(middle.get("name"))
        folded = name.casefold()
        if folded in seen_middle:
            raise ValueError("同じ中分類を重複して指定できません。")
        seen_middle.add(folded)
        small_values = middle.get("smallTags", [])
        if not isinstance(small_values, list) or len(small_values) > 20:
            raise ValueError("小分類は中分類ごとに20個以内にしてください。")
        small_tags = []
        seen_small: set[str] = set()
        for small in small_values:
            small_tag = _tag(small)
            folded_small = small_tag.casefold()
            if folded_small in seen_small:
                raise ValueError("同じ中分類内の小分類は重複できません。")
            seen_small.add(folded_small)
            small_tags.append(small_tag)
        middle_tags.append({"name": name, "smallTags": small_tags})
    return {"major": major, "middleTags": middle_tags}


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS image_style_tags ("
        "sha256 TEXT PRIMARY KEY, major TEXT NOT NULL, middle_tags_json TEXT NOT NULL, "
        "updated_at INTEGER NOT NULL)"
    )
    return connection


def get_assignment(sha256: str, db_path: Path = DEFAULT_DB_PATH) -> dict | None:
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT major, middle_tags_json, updated_at FROM image_style_tags WHERE sha256 = ?",
            (sha256,),
        ).fetchone()
    if row is None:
        return None
    return {"major": row[0], "middleTags": json.loads(row[1]), "updatedAt": row[2]}


def put_assignment(sha256: str, value: object, db_path: Path = DEFAULT_DB_PATH) -> dict:
    assignment = normalize_assignment(value)
    updated_at = int(time.time())
    with closing(_connect(db_path)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO image_style_tags (sha256, major, middle_tags_json, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(sha256) DO UPDATE SET "
                "major = excluded.major, middle_tags_json = excluded.middle_tags_json, "
                "updated_at = excluded.updated_at",
                (sha256, assignment["major"], json.dumps(assignment["middleTags"], ensure_ascii=False), updated_at),
            )
    return {**assignment, "updatedAt": updated_at}


def delete_assignment(sha256: str, db_path: Path = DEFAULT_DB_PATH) -> None:
    with closing(_connect(db_path)) as connection:
        with connection:
            connection.execute("DELETE FROM image_style_tags WHERE sha256 = ?", (sha256,))
