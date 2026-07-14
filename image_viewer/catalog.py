from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

from app.utils.db import get_db
from PIL import Image, ImageOps


CATALOG_ENABLED = os.environ.get("IMAGE_VIEWER_CATALOG_ENABLED", "").lower() in {
    "1", "true", "yes", "on",
}
STORE_ROOT = Path(
    os.environ.get("IMAGE_VIEWER_STORE_DIR", "/mnt/mfu/image_store")
).expanduser()
ORIGINAL_ROOT = STORE_ROOT / "originals"
THUMBNAIL_ROOT = STORE_ROOT / "thumbnails"
ROOT_FOLDER_ID = 1


class CatalogError(RuntimeError):
    pass


class CatalogNotFound(CatalogError):
    pass


class CatalogConflict(CatalogError):
    pass


class CatalogDuplicate(CatalogConflict):
    def __init__(self, record: dict):
        self.record = record
        super().__init__(
            f"Duplicate file already exists: {record.get('path') or record.get('uuid')}"
        )


def _rows(cursor) -> list[dict]:
    return list(cursor.fetchall() or [])


def _normalise_virtual_path(value: str) -> str:
    value = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(value)
    if value and (".." in path.parts or "." in path.parts):
        raise CatalogError("Invalid virtual path")
    return "" if value in {"", "."} else path.as_posix()


def _folder_maps(cursor) -> tuple[dict[int, dict], dict[str, int]]:
    cursor.execute(
        "SELECT id, folder_uuid, parent_id, folder_name, status "
        "FROM image_viewer_folders ORDER BY id"
    )
    rows = {int(row["id"]): row for row in _rows(cursor)}
    paths: dict[int, str] = {ROOT_FOLDER_ID: ""}

    def build(folder_id: int, visiting: set[int] | None = None) -> str | None:
        if folder_id in paths:
            return paths[folder_id]
        visiting = visiting or set()
        if folder_id in visiting or folder_id not in rows:
            raise CatalogError("Invalid folder hierarchy")
        visiting.add(folder_id)
        row = rows[folder_id]
        if row["status"] != "active":
            visiting.remove(folder_id)
            return None
        parent = build(int(row["parent_id"]), visiting)
        if parent is None:
            visiting.remove(folder_id)
            return None
        result = f"{parent}/{row['folder_name']}".strip("/")
        paths[folder_id] = result
        visiting.remove(folder_id)
        return result

    for folder_id, row in rows.items():
        if row["status"] != "active":
            continue
        build(folder_id)
    return rows, {path: folder_id for folder_id, path in paths.items()}


def _folder_id(cursor, folder_path: str) -> int:
    _, by_path = _folder_maps(cursor)
    normalised = _normalise_virtual_path(folder_path)
    try:
        return by_path[normalised]
    except KeyError as exc:
        raise CatalogNotFound(f"Folder not found: {normalised}") from exc


def _record(row: dict, folder_path: str) -> dict:
    name = row["display_name"]
    virtual_path = f"{folder_path}/{name}".strip("/")
    return {
        "id": int(row["id"]),
        "uuid": row["file_uuid"],
        "name": name,
        "path": virtual_path,
        "folder": folder_path,
        "mediaType": row["media_type"],
        "size": int(row["file_size"]),
        "mtime": int(row["mtime_epoch"]),
        "url": f"/image_viewer/files/{row['file_uuid']}",
        "thumbUrl": (
            f"/image_viewer/thumbs/{row['file_uuid']}"
            if row.get("thumbnail_relpath")
            else None
        ),
        "hasThumb": bool(row.get("thumbnail_relpath")),
    }


def list_payload(folder: str | None = None) -> dict:
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        _, by_path = _folder_maps(cursor)
        path_by_id = {folder_id: path for path, folder_id in by_path.items()}
        params: tuple = ()
        where = "f.status = 'active'"
        if folder is not None:
            folder_id = _folder_id(cursor, folder)
            where += " AND f.folder_id = %s"
            params = (folder_id,)
        cursor.execute(
            "SELECT f.*, UNIX_TIMESTAMP(f.file_mtime) AS mtime_epoch "
            "FROM image_viewer_files f "
            f"WHERE {where} ORDER BY f.folder_id, f.display_name",
            params,
        )
        images = [
            _record(row, path_by_id[int(row["folder_id"])])
            for row in _rows(cursor)
        ]
        folders = sorted(
            by_path.keys(), key=lambda value: (value.count("/"), value.lower())
        )
        return {
            "ok": True,
            "catalog": True,
            "root": str(STORE_ROOT),
            "folders": folders,
            "images": images,
            "generatedAt": datetime.now().timestamp(),
            "completedAt": datetime.now().timestamp(),
        }
    finally:
        conn.close()


def create_folder(parent_path: str, name: str) -> str:
    parent_path = _normalise_virtual_path(parent_path)
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise CatalogError("Invalid folder name")
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        parent_id = _folder_id(cursor, parent_path)
        try:
            cursor.execute(
                "INSERT INTO image_viewer_folders "
                "(folder_uuid, parent_id, folder_name) VALUES (%s, %s, %s)",
                (str(uuid.uuid4()), parent_id, name),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if getattr(exc, "errno", None) == 1062:
                raise CatalogConflict("A folder with that name already exists") from exc
            raise
        return f"{parent_path}/{name}".strip("/")
    finally:
        conn.close()


def resolve_file(file_uuid: str) -> tuple[Path, dict]:
    try:
        file_uuid = str(uuid.UUID(file_uuid))
    except ValueError as exc:
        raise CatalogNotFound("File not found") from exc
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM image_viewer_files "
            "WHERE file_uuid = %s AND status = 'active'",
            (file_uuid,),
        )
        row = cursor.fetchone()
        if not row:
            raise CatalogNotFound("File not found")
        path = (ORIGINAL_ROOT / row["storage_relpath"]).resolve()
        path.relative_to(ORIGINAL_ROOT.resolve())
        return path, row
    finally:
        conn.close()


def resolve_thumbnail(file_uuid: str) -> Path:
    _, row = resolve_file(file_uuid)
    if not row.get("thumbnail_relpath"):
        raise CatalogNotFound("Thumbnail not found")
    path = (THUMBNAIL_ROOT / row["thumbnail_relpath"]).resolve()
    path.relative_to(THUMBNAIL_ROOT.resolve())
    return path


def _next_display_name(cursor, folder_id: int, suffix: str) -> str:
    cursor.execute(
        "SELECT display_name FROM image_viewer_files "
        "WHERE folder_id = %s AND status <> 'trash'",
        (folder_id,),
    )
    used = {row["display_name"].lower() for row in _rows(cursor)}
    number = 1
    while f"{number}{suffix}".lower() in used:
        number += 1
    return f"{number}{suffix}"


def store_file(
    source: Path,
    folder_path: str,
    display_name: str | None = None,
    *,
    move_source: bool = False,
    checksum: bytes | None = None,
) -> dict:
    source = Path(source)
    if not source.is_file():
        raise CatalogNotFound(f"Source file not found: {source}")
    checksum = checksum or checksum_file(source)
    suffix = source.suffix.lower()
    file_uuid = str(uuid.uuid4())
    storage_relpath = f"{file_uuid[:2]}/{file_uuid}{suffix}"
    destination = ORIGINAL_ROOT / storage_relpath
    destination.parent.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    lock_name = f"mfu_image_sha256_{checksum.hex()}"
    lock_acquired = False
    cursor = None
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT GET_LOCK(%s, 30) AS acquired", (lock_name,))
        lock_acquired = bool((cursor.fetchone() or {}).get("acquired"))
        if not lock_acquired:
            raise CatalogError("Timed out while checking for duplicate files")
        cursor.execute(
            "SELECT f.*, UNIX_TIMESTAMP(f.file_mtime) AS mtime_epoch "
            "FROM image_viewer_files f "
            "WHERE f.checksum_sha256 = %s "
            "AND f.status IN ('active', 'processing') "
            "ORDER BY (f.status = 'active') DESC, f.id LIMIT 1",
            (checksum,),
        )
        existing = cursor.fetchone()
        if existing and existing["status"] == "processing":
            pending_path = ORIGINAL_ROOT / existing["storage_relpath"]
            if pending_path.is_file() and checksum_file(pending_path) == checksum:
                cursor.execute(
                    "UPDATE image_viewer_files SET status = 'active' WHERE id = %s",
                    (existing["id"],),
                )
                conn.commit()
                existing["status"] = "active"
            else:
                pending_path.unlink(missing_ok=True)
                cursor.execute(
                    "DELETE FROM image_viewer_files "
                    "WHERE id = %s AND status = 'processing'",
                    (existing["id"],),
                )
                conn.commit()
                existing = None
        if existing:
            _, by_path = _folder_maps(cursor)
            path_by_id = {folder_id: path for path, folder_id in by_path.items()}
            existing_folder = path_by_id.get(int(existing["folder_id"]), "")
            raise CatalogDuplicate(_record(existing, existing_folder))
        folder_id = _folder_id(cursor, folder_path)
        display_name = display_name or _next_display_name(cursor, folder_id, suffix)
        if "/" in display_name or "\\" in display_name:
            raise CatalogError("Invalid file name")
        cursor.execute(
            "INSERT INTO image_viewer_files "
            "(file_uuid, folder_id, display_name, storage_relpath, extension, "
            "media_type, mime_type, file_size, file_mtime, checksum_sha256, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FROM_UNIXTIME(%s), %s, 'processing')",
            (
                file_uuid,
                folder_id,
                display_name,
                storage_relpath,
                suffix,
                "video" if suffix in {".mp4", ".webm", ".mov", ".m4v"} else "image",
                mimetypes.guess_type(display_name)[0],
                source.stat().st_size,
                source.stat().st_mtime,
                checksum,
            ),
        )
        try:
            if move_source:
                source.replace(destination)
            else:
                shutil.copy2(source, destination)
            cursor.execute(
                "UPDATE image_viewer_files SET status = 'active' WHERE file_uuid = %s",
                (file_uuid,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            destination.unlink(missing_ok=True)
            raise
        cursor.execute(
            "SELECT f.*, UNIX_TIMESTAMP(f.file_mtime) AS mtime_epoch "
            "FROM image_viewer_files f WHERE file_uuid = %s",
            (file_uuid,),
        )
        return _record(cursor.fetchone(), _normalise_virtual_path(folder_path))
    finally:
        if lock_acquired and cursor is not None:
            try:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            except Exception:
                pass
        conn.close()


def checksum_file(path: Path) -> bytes:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest()


def _file_row_for_virtual_path(cursor, path: str) -> tuple[dict, str]:
    parent_path, name = _split_entry_path(path)
    folder_id = _folder_id(cursor, parent_path)
    cursor.execute(
        "SELECT f.*, UNIX_TIMESTAMP(f.file_mtime) AS mtime_epoch "
        "FROM image_viewer_files f "
        "WHERE f.folder_id = %s AND f.display_name = %s AND f.status = 'active'",
        (folder_id, name),
    )
    row = cursor.fetchone()
    if not row:
        raise CatalogNotFound("File not found")
    return row, parent_path


def file_properties(path: str) -> dict:
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        row, folder_path = _file_row_for_virtual_path(cursor, path)
        real_path = (ORIGINAL_ROOT / row["storage_relpath"]).resolve()
        real_path.relative_to(ORIGINAL_ROOT.resolve())
        stat = real_path.stat()
        width = height = None
        if str(row.get("media_type") or "") == "image":
            try:
                with Image.open(real_path) as image:
                    width, height = image.size
            except Exception:
                pass
        record = _record(row, folder_path)
        suffix = Path(record["name"]).suffix
        return {
            "ok": True,
            "entry": record,
            "name": record["name"],
            "stem": Path(record["name"]).stem,
            "extension": suffix,
            "virtualFolder": folder_path,
            "virtualPath": record["path"],
            "realPath": real_path.as_posix(),
            "size": int(stat.st_size),
            "created": int(stat.st_ctime),
            "modified": int(stat.st_mtime),
            "accessed": int(stat.st_atime),
            "mediaType": row.get("media_type") or record.get("mediaType"),
            "mimeType": row.get("mime_type") or mimetypes.guess_type(record["name"])[0],
            "width": width,
            "height": height,
            "sha256": bytes(row["checksum_sha256"]).hex() if row.get("checksum_sha256") else checksum_file(real_path).hex(),
        }
    finally:
        conn.close()


def duplicate_groups() -> dict:
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        _, by_path = _folder_maps(cursor)
        path_by_id = {folder_id: path for path, folder_id in by_path.items()}
        cursor.execute(
            "SELECT f.*, UNIX_TIMESTAMP(f.file_mtime) AS mtime_epoch "
            "FROM image_viewer_files f "
            "JOIN ("
            " SELECT checksum_sha256 FROM image_viewer_files "
            " WHERE status = 'active' AND checksum_sha256 IS NOT NULL "
            " GROUP BY checksum_sha256 HAVING COUNT(id) > 1"
            ") duplicates ON duplicates.checksum_sha256 = f.checksum_sha256 "
            "WHERE f.status = 'active' "
            "ORDER BY f.checksum_sha256, f.id"
        )
        grouped: dict[bytes, list[dict]] = {}
        for row in _rows(cursor):
            grouped.setdefault(bytes(row["checksum_sha256"]), []).append(row)
        groups = []
        extra_copies = 0
        reclaimable_bytes = 0
        for checksum, rows in grouped.items():
            records = [
                _record(row, path_by_id.get(int(row["folder_id"]), ""))
                for row in rows
            ]
            extras = max(0, len(records) - 1)
            extra_copies += extras
            reclaimable_bytes += extras * int(rows[0]["file_size"])
            groups.append(
                {
                    "sha256": checksum.hex(),
                    "count": len(records),
                    "fileSize": int(rows[0]["file_size"]),
                    "reclaimableBytes": extras * int(rows[0]["file_size"]),
                    "files": records,
                }
            )
        groups.sort(key=lambda group: (-group["reclaimableBytes"], group["sha256"]))
        return {
            "ok": True,
            "kind": "duplicates",
            "groupCount": len(groups),
            "extraCopies": extra_copies,
            "reclaimableBytes": reclaimable_bytes,
            "groups": groups,
        }
    finally:
        conn.close()


def _split_entry_path(path: str) -> tuple[str, str]:
    normalised = _normalise_virtual_path(path)
    pure = PurePosixPath(normalised)
    if not normalised or not pure.name:
        raise CatalogError("Invalid entry path")
    parent = "" if str(pure.parent) == "." else pure.parent.as_posix()
    return parent, pure.name


def rename_entry(path: str, new_name: str, entry_type: str) -> str:
    parent_path, old_name = _split_entry_path(path)
    if not new_name or "/" in new_name or "\\" in new_name or new_name in {".", ".."}:
        raise CatalogError("Invalid name")
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        parent_id = _folder_id(cursor, parent_path)
        try:
            if entry_type == "folder":
                cursor.execute(
                    "UPDATE image_viewer_folders SET folder_name = %s "
                    "WHERE parent_id = %s AND folder_name = %s",
                    (new_name, parent_id, old_name),
                )
            else:
                cursor.execute(
                    "UPDATE image_viewer_files SET display_name = %s "
                    "WHERE folder_id = %s AND display_name = %s AND status = 'active'",
                    (new_name, parent_id, old_name),
                )
            if cursor.rowcount != 1:
                raise CatalogNotFound("Entry not found")
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if getattr(exc, "errno", None) == 1062:
                raise CatalogConflict("An entry with that name already exists") from exc
            raise
        return f"{parent_path}/{new_name}".strip("/")
    finally:
        conn.close()


def move_entry(path: str, destination: str, entry_type: str) -> str:
    source_parent, name = _split_entry_path(path)
    destination = _normalise_virtual_path(destination)
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        source_parent_id = _folder_id(cursor, source_parent)
        destination_id = _folder_id(cursor, destination)
        try:
            if entry_type == "folder":
                cursor.execute(
                    "SELECT id FROM image_viewer_folders "
                    "WHERE parent_id = %s AND folder_name = %s AND status = 'active'",
                    (source_parent_id, name),
                )
                row = cursor.fetchone()
                if not row:
                    raise CatalogNotFound("Folder not found")
                folder_id = int(row["id"])
                cursor.execute(
                    "WITH RECURSIVE descendants AS ("
                    " SELECT id FROM image_viewer_folders WHERE id = %s"
                    " UNION ALL"
                    " SELECT f.id FROM image_viewer_folders f"
                    " JOIN descendants d ON f.parent_id = d.id"
                    ") SELECT id FROM descendants WHERE id = %s",
                    (folder_id, destination_id),
                )
                if cursor.fetchone():
                    raise CatalogConflict("A folder cannot be moved into itself")
                cursor.execute(
                    "UPDATE image_viewer_folders SET parent_id = %s WHERE id = %s",
                    (destination_id, folder_id),
                )
            else:
                cursor.execute(
                    "UPDATE image_viewer_files SET folder_id = %s "
                    "WHERE folder_id = %s AND display_name = %s AND status = 'active'",
                    (destination_id, source_parent_id, name),
                )
                if cursor.rowcount != 1:
                    raise CatalogNotFound("File not found")
            conn.commit()
        except Exception as exc:
            conn.rollback()
            if getattr(exc, "errno", None) == 1062:
                raise CatalogConflict("An entry with that name already exists") from exc
            raise
        return f"{destination}/{name}".strip("/")
    finally:
        conn.close()


def trash_entry(path: str, entry_type: str) -> None:
    parent_path, name = _split_entry_path(path)
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        parent_id = _folder_id(cursor, parent_path)
        if entry_type == "folder":
            cursor.execute(
                "SELECT id FROM image_viewer_folders "
                "WHERE parent_id = %s AND folder_name = %s AND status = 'active'",
                (parent_id, name),
            )
            row = cursor.fetchone()
            if not row:
                raise CatalogNotFound("Folder not found")
            folder_id = int(row["id"])
            cursor.execute(
                "SELECT 1 FROM image_viewer_folders "
                "WHERE parent_id = %s AND status = 'active' LIMIT 1",
                (folder_id,),
            )
            has_folders = cursor.fetchone() is not None
            cursor.execute(
                "SELECT 1 FROM image_viewer_files "
                "WHERE folder_id = %s AND status = 'active' LIMIT 1",
                (folder_id,),
            )
            if has_folders or cursor.fetchone():
                raise CatalogConflict("The folder is not empty")
            cursor.execute(
                "UPDATE image_viewer_folders "
                "SET status = 'trash', trashed_at = NOW(6), "
                "folder_name = CONCAT(folder_name, '__trash__', LEFT(folder_uuid, 8)) "
                "WHERE id = %s",
                (folder_id,),
            )
        else:
            cursor.execute(
                "UPDATE image_viewer_files "
                "SET status = 'trash', trashed_at = NOW(6), "
                "display_name = CONCAT(display_name, '__trash__', LEFT(file_uuid, 8)) "
                "WHERE folder_id = %s AND display_name = %s AND status = 'active'",
                (parent_id, name),
            )
            if cursor.rowcount != 1:
                raise CatalogNotFound("File not found")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_thumbnail(file_uuid: str, thumbnail: Path) -> str:
    file_uuid = str(uuid.UUID(file_uuid))
    relpath = f"{file_uuid[:2]}/{file_uuid}.webp"
    destination = THUMBNAIL_ROOT / relpath
    destination.parent.mkdir(parents=True, exist_ok=True)
    Path(thumbnail).replace(destination)
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE image_viewer_files SET thumbnail_relpath = %s "
            "WHERE file_uuid = %s AND status = 'active'",
            (relpath, file_uuid),
        )
        if cursor.rowcount != 1:
            destination.unlink(missing_ok=True)
            raise CatalogNotFound("File not found")
        conn.commit()
        return relpath
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def next_number(folder_path: str) -> dict:
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        folder_id = _folder_id(cursor, folder_path)
        cursor.execute(
            "SELECT display_name FROM image_viewer_files "
            "WHERE folder_id = %s AND status = 'active'",
            (folder_id,),
        )
        highest = 0
        for row in _rows(cursor):
            stem = Path(row["display_name"]).stem
            if stem.isdigit():
                highest = max(highest, int(stem))
        return {"nextNumber": highest + 1, "digits": max(3, len(str(highest + 1)))}
    finally:
        conn.close()


def generate_thumbnail(file_uuid: str) -> bool:
    source, row = resolve_file(file_uuid)
    fd, temp_name = tempfile.mkstemp(prefix="mfu_thumb_", suffix=".webp")
    os.close(fd)
    target = Path(temp_name)
    try:
        if row["media_type"] == "video":
            subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", "00:00:01", "-i", str(source),
                    "-frames:v", "1", "-vf",
                    "scale=360:360:force_original_aspect_ratio=decrease",
                    str(target),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
        else:
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                image.thumbnail((360, 360), resample)
                if image.mode not in ("RGB", "RGBA"):
                    image = image.convert("RGBA")
                image.save(target, "WEBP", quality=82, method=6)
        set_thumbnail(file_uuid, target)
        return True
    except Exception:
        target.unlink(missing_ok=True)
        return False


def thumbnail_candidates(folder_path: str = "", force: bool = False) -> list[str]:
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        folder_id = _folder_id(cursor, folder_path)
        where = "folder_id = %s AND status = 'active'"
        if not force:
            where += " AND thumbnail_relpath IS NULL"
        cursor.execute(
            f"SELECT file_uuid FROM image_viewer_files WHERE {where} ORDER BY id",
            (folder_id,),
        )
        return [row["file_uuid"] for row in _rows(cursor)]
    finally:
        conn.close()
