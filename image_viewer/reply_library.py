"""Read-only, owner-scoped projection of the upload-history reply images."""

import hashlib
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from flask import url_for
from werkzeug.exceptions import Forbidden, NotFound

from app.utils.db import get_db


# A '..' segment is not accepted by the writable image catalog. This is
# only an opaque Explorer key; it is never joined to a filesystem path.
ROOT = ".."
ROOT_LABEL = "加工バック"
JST = timezone(timedelta(hours=9), "JST")


def is_reply_folder(folder):
    return folder == ROOT or folder.startswith(f"{ROOT}/")


def parent_id(folder):
    if folder == ROOT:
        return None
    suffix = folder.removeprefix(f"{ROOT}/")
    if not suffix.isascii() or not suffix.isdigit() or not 1 <= len(suffix) <= 10:
        raise ValueError("Invalid reply folder")
    return int(suffix)


def _parents(username):
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        try:
            owner = "" if username == "admin" else "AND u.username=%s"
            cur.execute(
                f"""
                SELECT u.id, u.uuid, u.title, u.date, COUNT(f.id) AS image_count,
                       MAX(f.id) AS latest_file, MAX(r.posted_at) AS latest_posted
                  FROM uploads u
                  JOIN layer_upload_replies r ON r.upload_id=u.id
                  JOIN layer_upload_reply_files f ON f.reply_id=r.id AND f.file_kind='image'
                 WHERE u.layer_deleted_at IS NULL {owner}
                 GROUP BY u.id, u.uuid, u.title, u.date
                 ORDER BY u.id
                """,
                () if username == "admin" else (username,),
            )
            return {row["id"]: row for row in cur.fetchall()}
        finally:
            cur.close()
    finally:
        db.close()


def _shooting_date(value):
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return value.date() if isinstance(value, datetime) else value if isinstance(value, date) else None


def _date_label(day):
    return day.strftime("%Y年%m月%d日") if day is not None else "撮影日未設定"


def _parent_label(row):
    return f'{_date_label(_shooting_date(row["date"]))}　{row["title"] or "（タイトルなし）"}'


def _folder_labels(parents):
    return {ROOT: ROOT_LABEL, **{f"{ROOT}/{i}": _parent_label(row) for i, row in parents.items()}}


def folder_labels(username):
    return _folder_labels(_parents(username))


def _version(parents):
    return hashlib.sha256(repr(parents).encode()).hexdigest()


def version(username, folder):
    selected = parent_id(folder)
    parents = _parents(username)
    if selected is not None and selected not in parents:
        raise LookupError("Reply folder not found")
    return _version(parents)


def _images(username, selected):
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        try:
            clauses = ["u.layer_deleted_at IS NULL", "f.file_kind='image'"]
            params = []
            if username != "admin":
                clauses.append("u.username=%s")
                params.append(username)
            if selected is not None:
                clauses.append("u.id=%s")
                params.append(selected)
            cur.execute(
                f"""
                SELECT u.id AS parent_id, u.uuid AS parent_uuid, r.reply_uuid,
                       r.id AS reply_id, r.posted_at, f.id, f.filename, f.sort_order
                  FROM uploads u JOIN layer_upload_replies r ON r.upload_id=u.id
                  JOIN layer_upload_reply_files f ON f.reply_id=r.id
                 WHERE {' AND '.join(clauses)}
                """,
                tuple(params),
            )
            return cur.fetchall()
        finally:
            cur.close()
    finally:
        db.close()


def _order(rows, parents, group_by, descending):
    capture_dates = {i: _shooting_date(parent["date"]) for i, parent in parents.items()}

    def ordered_by_capture(entries, get_day):
        known, unknown = [], []
        for entry in entries:
            day = get_day(entry)
            if day is None:
                unknown.append(entry)
            else:
                known.append((day, entry))
        known.sort(key=lambda pair: pair[0], reverse=descending)
        return [entry for _, entry in known] + unknown

    # The upload timestamp breaks ties; file order within one submission
    # is preserved in both directions.
    rows.sort(key=lambda r: (r["sort_order"], r["id"]))
    rows.sort(key=lambda r: (r["posted_at"], r["reply_id"]), reverse=descending)
    if group_by == "none":
        return ordered_by_capture(rows, lambda row: capture_dates[row["parent_id"]]), []
    buckets = defaultdict(list)
    for row in rows:
        if group_by == "replyParent":
            key = row["parent_id"]
        elif group_by == "replyCaptured":
            key = capture_dates[row["parent_id"]]
        else:
            key = row["posted_at"].date()
        buckets[key].append(row)
    if group_by == "replyParent":
        keys = ordered_by_capture(sorted(buckets, reverse=descending), capture_dates.get)
    else:
        keys = ordered_by_capture(buckets, lambda day: day)
    ordered, groups = [], []
    for key in keys:
        bucket = buckets[key]
        label = _parent_label(parents[key]) if group_by == "replyParent" else _date_label(key)
        groups.append({"key": str(key) if key is not None else "unknown", "label": label, "start": len(ordered), "count": len(bucket)})
        ordered.extend(bucket)
    return ordered, groups


def _record(row, parent):
    args = {"uuid": row["parent_uuid"], "reply_uuid": row["reply_uuid"], "filename": row["filename"]}
    folder = f'{ROOT}/{row["parent_id"]}'
    posted = row["posted_at"]
    shooting_date = _shooting_date(parent["date"])
    timestamp = (posted if posted.tzinfo else posted.replace(tzinfo=JST)).timestamp()
    return {
        "name": row["filename"], "path": f'{folder}/{row["id"]}', "folder": folder,
        "mediaType": "image", "readOnly": True, "size": 0, "mtime": timestamp,
        "capturedAt": datetime.combine(shooting_date, time(), JST).timestamp() if shooting_date else None,
        "registeredAt": timestamp, "url": url_for("upload_history.layer_upload_image", **args),
        "thumbUrl": url_for("upload_history.layer_upload_thumbnail", **args), "hasThumb": True,
    }


def transfer_source(path, username):
    """Resolve an opaque reply-library key using registered IDs, not paths."""
    match = re.fullmatch(r"\.\./([0-9]{1,10})/([0-9]{1,20})", path)
    if not match:
        raise ValueError("加工バックの画像を選択してください。")
    upload_id, file_id = map(int, match.groups())
    if not 0 < upload_id < 2**32 or not 0 < file_id < 2**64:
        raise ValueError("加工バックの画像を選択してください。")
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        try:
            cur.execute(
                """
                SELECT u.uuid, r.reply_uuid, f.filename
                  FROM uploads u JOIN layer_upload_replies r ON r.upload_id=u.id
                  JOIN layer_upload_reply_files f ON f.reply_id=r.id
                 WHERE u.id=%s AND f.id=%s AND f.file_kind='image' AND u.layer_deleted_at IS NULL
                """,
                (upload_id, file_id),
            )
            row = cur.fetchone()
        finally:
            cur.close()
    finally:
        db.close()
    if row:
        from app.utils.upload_history import resolve_layer_image_path

        try:
            source = resolve_layer_image_path(row["uuid"], row["reply_uuid"], row["filename"], username)
            return source, row["filename"]
        except (Forbidden, NotFound):
            pass
    raise ValueError("加工バックの画像が見つかりません。")


def list_payload(username, folder, *, sort, page, per_page, center, group_by):
    selected = parent_id(folder)
    if group_by not in {"none", "replyCaptured", "replyParent" if selected is None else "replyUploaded"}:
        raise ValueError("Invalid reply grouping")
    parents = _parents(username)
    if selected is not None and selected not in parents:
        raise LookupError("Reply folder not found")
    # The two reads can race a newly completed reply. Keep only parents in this
    # snapshot; a later version check refreshes the tree and grid together.
    rows = [row for row in _images(username, selected) if row["parent_id"] in parents]
    rows, groups = _order(rows, parents, group_by, sort == "desc")
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    if center is not None:
        center = min(center, max(0, total - 1))
        page = (center // per_page) + 1
        offset = min(max(0, center - per_page // 2), max(0, total - per_page))
    else:
        page = min(page, pages)
        offset = max(0, (page - 1) * per_page - min(150, per_page // 4))
    return {
        "ok": True, "folder": folder, "replyFolders": _folder_labels(parents),
        "images": [_record(row, parents[row["parent_id"]]) for row in rows[offset:offset + per_page]],
        "groups": groups, "version": _version(parents),
        "pagination": {
            "page": page, "perPage": per_page, "total": total, "pages": pages,
            "hasMore": offset + per_page < total, "offset": offset,
            "center": center if center is not None else min(max(0, total - 1), offset + per_page // 2),
            "mode": "window" if center is not None else "page",
        },
    }
