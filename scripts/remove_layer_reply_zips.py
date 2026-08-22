from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid as uuidlib
from pathlib import Path


APP_PARENT = Path(__file__).resolve().parents[2]
if str(APP_PARENT) not in sys.path:
    sys.path.insert(0, str(APP_PARENT))

from app.utils.db import get_db


def _safe_zip_dir(root: Path, upload_uuid: str, reply_uuid: str) -> Path:
    target = (root / upload_uuid / reply_uuid / "zip").resolve()
    expected_parent = (root / upload_uuid / reply_uuid).resolve()
    if target.parent != expected_parent or target.name != "zip":
        raise RuntimeError(f"安全でないZIPディレクトリです: {target}")
    return target


def _safe_zip_file(root: Path, upload_uuid: str, reply_uuid: str, filename: str) -> Path:
    candidate = str(filename or "").strip()
    if not candidate or Path(candidate).name != candidate:
        raise RuntimeError(f"安全でないZIPファイル名です: {candidate!r}")
    zip_dir = _safe_zip_dir(root, upload_uuid, reply_uuid)
    target = (zip_dir / candidate).resolve()
    if target.parent != zip_dir:
        raise RuntimeError(f"ZIPファイルが保存領域外を指しています: {target}")
    return target


def inspect(root: Path) -> dict:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT file.id, upload.uuid AS upload_uuid, reply.reply_uuid, file.filename
              FROM layer_upload_reply_files AS file
              JOIN layer_upload_replies AS reply ON reply.id=file.reply_id
              JOIN uploads AS upload ON upload.id=reply.upload_id
             WHERE file.file_kind='zip'
             ORDER BY file.id
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        db.close()

    expected_files = {
        _safe_zip_file(root, row["upload_uuid"], row["reply_uuid"], row["filename"])
        for row in rows
    }
    zip_dirs = sorted(path.resolve() for path in root.glob("*/*/zip") if path.is_dir())
    actual_files = {
        path.resolve()
        for zip_dir in zip_dirs
        for path in zip_dir.rglob("*")
        if path.is_file()
    }
    missing_files = sorted(str(path) for path in expected_files - actual_files)
    unmanaged_files = sorted(str(path) for path in actual_files - expected_files)
    if missing_files or unmanaged_files:
        raise RuntimeError(
            "DBとZIP実体が一致しません: "
            f"missing={missing_files[:10]} unmanaged={unmanaged_files[:10]}"
        )
    return {
        "db_rows": len(rows),
        "zip_dirs": len(zip_dirs),
        "zip_files": len(actual_files),
        "zip_bytes": sum(path.stat().st_size for path in actual_files),
        "rows": rows,
        "directories": zip_dirs,
    }


def apply_removal(root: Path, inspection: dict) -> dict:
    token = uuidlib.uuid4().hex
    renamed: list[tuple[Path, Path]] = []
    for zip_dir in inspection["directories"]:
        pending = zip_dir.with_name(f"zip.pending-delete-{token}")
        if pending.exists():
            raise RuntimeError(f"退避先が既に存在します: {pending}")
        zip_dir.replace(pending)
        renamed.append((zip_dir, pending))

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("DELETE FROM layer_upload_reply_files WHERE file_kind='zip'")
        deleted_rows = max(0, int(cur.rowcount or 0))
        if deleted_rows != int(inspection["db_rows"]):
            raise RuntimeError(
                f"DB削除件数が一致しません: expected={inspection['db_rows']} actual={deleted_rows}"
            )
        db.commit()
    except Exception:
        db.rollback()
        for original, pending in reversed(renamed):
            if pending.exists() and not original.exists():
                pending.replace(original)
        raise
    finally:
        cur.close()
        db.close()

    for _original, pending in renamed:
        shutil.rmtree(pending)

    return {
        "deleted_db_rows": deleted_rows,
        "deleted_zip_dirs": len(renamed),
        "deleted_zip_files": int(inspection["zip_files"]),
        "released_bytes": int(inspection["zip_bytes"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="レイヤー返信のZIPだけを検証付きで削除します。")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/mnt/mfu/uploads/layer_uploads"),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    inspection = inspect(root)
    report = {
        "mode": "apply" if args.apply else "dry-run",
        "db_rows": inspection["db_rows"],
        "zip_dirs": inspection["zip_dirs"],
        "zip_files": inspection["zip_files"],
        "zip_bytes": inspection["zip_bytes"],
    }
    if args.apply:
        report.update(apply_removal(root, inspection))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
