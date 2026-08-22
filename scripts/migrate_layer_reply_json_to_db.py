from __future__ import annotations

import argparse
import json
import sys
import tarfile
from datetime import datetime
from pathlib import Path


APP_PARENT = Path(__file__).resolve().parents[2]
if str(APP_PARENT) not in sys.path:
    sys.path.insert(0, str(APP_PARENT))

from app.utils.db import get_db
from app.utils.layer_reply_store import (
    create_layer_reply,
    ensure_layer_reply_schema,
    get_layer_reply,
)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _stored_images(folder: Path) -> list[str]:
    original_dir = folder / "original"
    return sorted(
        entry.name
        for entry in original_dir.iterdir()
        if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS
    ) if original_dir.is_dir() else []


def _fetch_uploads(parent_uuids: list[str]) -> dict[str, dict]:
    if not parent_uuids:
        return {}
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        placeholders = ",".join(["%s"] * len(parent_uuids))
        cur.execute(
            f"SELECT id, uuid, title FROM uploads WHERE uuid IN ({placeholders})",
            parent_uuids,
        )
        return {row["uuid"]: row for row in cur.fetchall()}
    finally:
        cur.close()
        db.close()


def _load_sources(root: Path) -> list[dict]:
    reply_dirs = sorted(
        folder
        for parent in root.iterdir()
        if parent.is_dir()
        for folder in parent.iterdir()
        if folder.is_dir()
    ) if root.is_dir() else []
    parent_uuids = sorted({folder.parent.name for folder in reply_dirs})
    uploads = _fetch_uploads(parent_uuids)
    missing_uploads = sorted(set(parent_uuids) - set(uploads))
    if missing_uploads:
        raise RuntimeError(f"uploadsテーブルに親UUIDがありません: {missing_uploads}")

    sources: list[dict] = []
    for folder in reply_dirs:
        info_path = folder / "info.json"
        if not info_path.is_file():
            raise RuntimeError(f"info.jsonがありません: {folder}")
        info = json.loads(info_path.read_text(encoding="utf-8"))
        reply_uuid = str(info.get("reply_uuid") or "").strip()
        if not reply_uuid or reply_uuid != folder.name:
            raise RuntimeError(f"返信UUIDが一致しません: {info_path}")
        created_text = str(info.get("created") or "").strip()
        if not created_text:
            raise RuntimeError(f"投稿日時がありません: {info_path}")
        posted_at = datetime.fromisoformat(created_text)
        images = _stored_images(folder)
        json_images = [str(value) for value in (info.get("filenames") or [])]
        if sorted(json_images) != images:
            raise RuntimeError(f"JSONと保存画像が一致しません: {info_path}")
        upload = uploads[folder.parent.name]
        sources.append(
            {
                "upload_id": int(upload["id"]),
                "upload_uuid": folder.parent.name,
                "reply_uuid": reply_uuid,
                "title_snapshot": str(info.get("title") or upload.get("title") or "").strip(),
                "comment": str(info.get("comment") or ""),
                "posted_at": posted_at,
                "images": images,
                "info_path": info_path,
            }
        )
    return sources


def _verify_existing(source: dict, stored: dict) -> None:
    checks = {
        "upload_uuid": source["upload_uuid"],
        "reply_uuid": source["reply_uuid"],
        "title_snapshot": source["title_snapshot"],
        "comment": source["comment"],
        "posted_at": source["posted_at"],
        "images": source["images"],
    }
    for key, expected in checks.items():
        actual = stored.get(key)
        if actual != expected:
            raise RuntimeError(
                f"移行済みDBとJSONが一致しません: reply_uuid={source['reply_uuid']} "
                f"field={key} expected={expected!r} actual={actual!r}"
            )


def migrate(root: Path, *, apply: bool) -> tuple[list[dict], dict]:
    sources = _load_sources(root)
    if apply:
        ensure_layer_reply_schema()
    inserted = 0
    verified = 0
    for source in sources:
        stored = get_layer_reply(source["reply_uuid"]) if apply else None
        if stored:
            _verify_existing(source, stored)
            verified += 1
            continue
        if apply:
            create_layer_reply(
                upload_id=source["upload_id"],
                reply_uuid=source["reply_uuid"],
                title_snapshot=source["title_snapshot"],
                comment=source["comment"],
                posted_at=source["posted_at"],
                image_filenames=source["images"],
            )
            stored = get_layer_reply(source["reply_uuid"])
            if not stored:
                raise RuntimeError(f"DB登録後の返信を取得できません: {source['reply_uuid']}")
            _verify_existing(source, stored)
            inserted += 1
    report = {
        "mode": "apply" if apply else "dry-run",
        "replies": len(sources),
        "comments": sum(bool(source["comment"].strip()) for source in sources),
        "images": sum(len(source["images"]) for source in sources),
        "inserted": inserted,
        "verified_existing": verified,
    }
    return sources, report


def archive_and_remove_json(sources: list[dict], backup_path: Path) -> None:
    if not sources:
        raise RuntimeError("削除対象のinfo.jsonがありません。")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.exists():
        raise RuntimeError(f"バックアップ先が既に存在します: {backup_path}")
    with tarfile.open(backup_path, "w:gz") as archive:
        for source in sources:
            info_path = source["info_path"]
            archive.add(
                info_path,
                arcname=f"{source['upload_uuid']}/{source['reply_uuid']}/info.json",
            )
    with tarfile.open(backup_path, "r:gz") as archive:
        archived = [member for member in archive.getmembers() if member.isfile()]
    if len(archived) != len(sources):
        raise RuntimeError(
            f"バックアップ件数が一致しません: expected={len(sources)} actual={len(archived)}"
        )
    for source in sources:
        source["info_path"].unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="レイヤー返信のinfo.jsonをDBへ移行します。")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("/mnt/mfu/uploads/layer_uploads"),
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--remove-json", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    if args.remove_json and (not args.apply or not args.backup):
        parser.error("--remove-jsonには--applyと--backupが必要です。")

    sources, report = migrate(args.root.resolve(), apply=args.apply)
    if args.remove_json:
        archive_and_remove_json(sources, args.backup.resolve())
        report["json_removed"] = len(sources)
        report["backup"] = str(args.backup.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
