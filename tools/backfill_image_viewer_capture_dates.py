#!/usr/bin/env python3
"""Fill missing image/video capture dates in small, restartable batches."""
from __future__ import annotations

import argparse

from app.utils.db import get_db
from app.image_viewer import catalog


def run(limit: int) -> tuple[int, int]:
    conn = get_db()
    updated = scanned = 0
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, storage_relpath, media_type FROM image_viewer_files "
            "WHERE status='active' AND captured_at IS NULL ORDER BY id LIMIT %s",
            (limit,),
        )
        for row in cursor.fetchall() or []:
            scanned += 1
            path = catalog.ORIGINAL_ROOT / row["storage_relpath"]
            captured = catalog.capture_datetime(path, row["media_type"])
            if not captured:
                continue
            cursor.execute(
                "UPDATE image_viewer_files SET captured_at=%s WHERE id=%s AND captured_at IS NULL",
                (captured, row["id"]),
            )
            updated += cursor.rowcount
        conn.commit()
        return scanned, updated
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    scanned, updated = run(max(1, args.limit))
    print(f"scanned={scanned} updated={updated}")
