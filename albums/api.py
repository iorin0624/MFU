"""JSON API used by the incremental Vue migration of the album UI.

The current HTML routes remain authoritative for business workflows.  This
module deliberately reuses their database, event membership, upload,
processing and passkey helpers so the Vue client cannot drift into a weaker
authorization model.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import shutil
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

from flask import current_app, jsonify, request, send_file, session, url_for
from PIL import Image

from app.external_login_user.utils import _event_acl_role, is_withdrawn_ext_user
from app.utils.admin_passkey_stepup import require_admin_passkey
from app.utils.thumbs import enqueue_thumb_job
from app.utils.zip_stream import read_zip_progress, start_zip_entries_job

from .routes import (
    ALBUM_ROOT,
    MOVIE_ROOT,
    _fetch_album_meta,
    _fetch_album_process_status_map,
    _fetch_event_process_members,
    _get_ext_user_nickname,
    _grant_album_auth,
    _get_ext_user_by_social,
    _has_album_auth,
    _is_event_member_approved,
    _is_ext_logged_in,
    _revoke_album_auth,
    _uuid_bytes_to_str,
    add_child_row,
    album_bp,
    allowed_file,
    allowed_movie,
    find_latest_filename,
    db_exec,
    db_get_all,
    db_get_one,
    delete_album_row,
    delete_child_row,
    ensure_album_child_creator_schema,
    load_meta,
    request_process,
    release_lock_db,
    resolve_thumb_url,
    storage_child_dir,
    try_acquire_lock_db,
    update_process_status,
    upload_child,
    LOCK_TTL_SEC,
)


API_MAX_PAGE_SIZE = 200
_SAFE_FILENAME_RE = re.compile(r"^[^/\\\x00]+$")


def _json_value(value: Any):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        converted = _uuid_bytes_to_str(value)
        return converted or value.hex()
    return value


def _csrf_token() -> str:
    token = str(session.get("csrf_token") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _ok(**payload):
    return jsonify({"ok": True, **payload})


def _error(error: str, status: int, message: str | None = None, **payload):
    body = {"ok": False, "error": error, **payload}
    if message:
        body["message"] = message
    return jsonify(body), status


def _event_summary(event_id: int | None) -> dict | None:
    if not event_id:
        return None
    row = db_get_one(
        "SELECT id, event_uuid, title, starts_at, place_name FROM mfu_event WHERE id=%s LIMIT 1",
        (int(event_id),),
    )
    if not row:
        return None
    return {key: _json_value(value) for key, value in row.items()}


def _current_external_user_id() -> int | None:
    value = session.get("ext_user_id")
    if value is None:
        social_id = session.get("ext_user_social_id")
        if social_id:
            user = _get_ext_user_by_social(str(social_id))
            value = (user or {}).get("id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _album_context(album_id: str, *, require_view: bool = True) -> tuple[dict | None, tuple | None]:
    ensure_album_child_creator_schema()
    meta = load_meta(album_id)
    gate = _fetch_album_meta(album_id)
    if not meta or not gate:
        return None, _error("album_not_found", 404, "アルバムが存在しません。")

    username = str(session.get("user") or "")
    is_admin = username == "admin"
    is_owner = bool(username and username == str(meta.get("owner") or ""))
    access_mode = str(gate.get("access_mode") or "token")
    event_id = gate.get("event_id")
    acl_role = _event_acl_role(int(event_id), username) if event_id and username else None
    is_event_acl = bool(acl_role)
    event_member = False

    if access_mode == "event" and event_id and not (is_admin or is_owner or is_event_acl):
        event_member = _is_event_member_approved(int(event_id))
        if event_member:
            _grant_album_auth(album_id)
        else:
            _revoke_album_auth(album_id)
    elif is_admin or is_owner or is_event_acl:
        _grant_album_auth(album_id)

    has_session_access = _has_album_auth(album_id)
    can_view = bool(
        is_admin
        or is_owner
        or is_event_acl
        or event_member
        or (access_mode == "token" and has_session_access)
    )
    if require_view and not can_view:
        error = "event_album_auth_required" if access_mode == "event" else "album_auth_required"
        return None, _error(
            error,
            403,
            "このアルバムの閲覧認証が必要です。",
            authUrl=url_for("album.album_access", album_id=album_id),
        )

    if is_admin:
        role = "admin"
    elif is_event_acl:
        role = "event_acl"
    elif is_owner:
        role = "owner"
    elif event_member:
        role = "event_member"
    elif has_session_access:
        role = "token_viewer"
    else:
        role = "none"

    ctx = {
        "album_id": album_id,
        "meta": meta,
        "gate": gate,
        "username": username,
        "is_admin": is_admin,
        "is_owner": is_owner,
        "is_event_acl": is_event_acl,
        "event_acl_role": acl_role,
        "current_ext_user_id": _current_external_user_id(),
        "event_member": event_member,
        "can_view": can_view,
        "can_manage": bool(is_admin or is_owner or is_event_acl),
        "can_create_child": bool(is_admin or is_owner or is_event_acl or event_member),
        "can_manage_processing": bool(is_admin or is_owner or is_event_acl or event_member),
        "role": role,
    }
    return ctx, None


def _permissions(ctx: dict) -> dict:
    return {
        "role": ctx["role"],
        "canView": ctx["can_view"],
        "canUpload": ctx["can_create_child"],
        "canCreateChild": ctx["can_create_child"],
        "canChooseChildType": _can_choose_child_type(ctx),
        "canRename": ctx["can_manage"],
        "canDeleteMedia": ctx["can_manage"],
        "canManageChildren": ctx["can_manage"],
        "canDeleteAlbum": ctx["can_manage"],
        "canManageProcessing": ctx["can_manage_processing"],
        "deleteRequiresPasskey": ctx["is_admin"],
    }


def _can_choose_child_type(ctx: dict) -> bool:
    if ctx.get("is_admin") or ctx.get("is_owner") or ctx.get("event_acl_role") == "manager":
        return True
    event_id = ctx.get("gate", {}).get("event_id")
    user_id = ctx.get("current_ext_user_id")
    if not (ctx.get("event_member") and event_id and user_id):
        return False
    row = db_get_one(
        "SELECT is_host, is_subhost FROM mfu_event_member WHERE event_id=%s AND user_id=%s AND status='approved' AND COALESCE(is_canceled,0)=0 LIMIT 1",
        (event_id, user_id),
    ) or {}
    return bool(row.get("is_host") or row.get("is_subhost"))


def _validate_child_template(ctx: dict, name: str, mode: str) -> bool:
    if _can_choose_child_type(ctx):
        return True
    templates = {"【構図】": "normal", "【オフショ】": "normal", "【動画】": "movie", "【加工回し】": "process"}
    return any(name.startswith(prefix) and bool(name[len(prefix):].strip()) and mode == expected for prefix, expected in templates.items())


def _child_permissions(ctx: dict, child: dict) -> dict:
    creator_id = child.get("created_by_ext_user_id")
    current_ext_user_id = ctx.get("current_ext_user_id")
    created_by_current_user = bool(
        creator_id is not None
        and current_ext_user_id is not None
        and int(creator_id) == int(current_ext_user_id)
    )
    creator_can_manage = bool(created_by_current_user and ctx.get("event_member"))
    can_manage_child = bool(ctx["can_manage"] or creator_can_manage)
    process_upload = bool(child.get("mode") == "process" and ctx.get("can_manage_processing"))
    return {
        "canView": ctx["can_view"],
        "canDownload": ctx["can_view"],
        "canUpload": bool(can_manage_child or process_upload),
        "canRenameChild": can_manage_child,
        "canDeleteMedia": can_manage_child,
        "canDeleteChild": can_manage_child,
        "canRenameMedia": ctx["can_manage"],
        "createdByCurrentUser": created_by_current_user,
        "deleteRequiresPasskey": bool(ctx["is_admin"]),
    }


def _require_child_permission(ctx: dict, child: dict, permission: str):
    if not _child_permissions(ctx, child).get(permission):
        return _error("forbidden", 403, "この子アルバムを変更する権限がありません。")
    return None


def _audit_child_action(action: str, ctx: dict, child: dict, **details: Any) -> None:
    payload = {
        "action": action,
        "album_id": ctx.get("album_id"),
        "child_id": child.get("folder"),
        "actor_user": ctx.get("username") or None,
        "actor_ext_user_id": ctx.get("current_ext_user_id"),
        **details,
    }
    current_app.logger.info(
        "ALBUM_CHILD_ACTION %s",
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
    )


def _child(ctx: dict, child_id: str) -> dict | None:
    return next(
        (item for item in ctx["meta"].get("children", []) if str(item.get("folder")) == child_id),
        None,
    )


def _lock_payload(album_id: str, child_id: str) -> dict | None:
    row = db_get_one(
        "SELECT username, acquired_at, expires_at FROM album_locks WHERE album_id=%s AND child_id=%s LIMIT 1",
        (album_id, child_id),
    )
    if not row:
        return None
    payload = {key: _json_value(value) for key, value in row.items()}
    expires_at = row.get("expires_at")
    remaining = None
    if isinstance(expires_at, datetime):
        remaining = max(0, int((expires_at - datetime.now(expires_at.tzinfo)).total_seconds()))
    payload["remainingSeconds"] = remaining
    payload["expired"] = remaining == 0 if remaining is not None else False
    return payload


def _processing_payload(ctx: dict, child: dict) -> dict:
    album_id = ctx["album_id"]
    child_id = str(child["folder"])
    history_path = Path(storage_child_dir(album_id, child_id, child.get("mode"))) / "history.json"
    history = []
    if history_path.is_file():
        try:
            value = json.loads(history_path.read_text(encoding="utf-8"))
            history = value if isinstance(value, list) else []
        except Exception:
            history = []
    history = [item for item in history if isinstance(item, dict) and not is_withdrawn_ext_user(item)]

    members = []
    event_id = ctx["gate"].get("event_id")
    if event_id:
        statuses = _fetch_album_process_status_map(album_id, child_id)
        for member in _fetch_event_process_members(int(event_id)):
            try:
                user_id = int(member.get("user_id"))
            except (TypeError, ValueError):
                continue
            status = statuses.get(user_id, {})
            members.append({
                **{key: _json_value(value) for key, value in member.items()},
                "requestFlag": bool(int(status.get("request_flag", 0))),
                "completeFlag": bool(int(status.get("complete_flag", 0))),
                "updatedAt": _json_value(status.get("updated_at")),
            })

    current_id = _current_external_user_id()
    current = next((member for member in members if int(member.get("user_id") or 0) == current_id), None)
    requested_members = [member for member in members if member.get("requestFlag")]
    completed = bool(requested_members) and all(member.get("completeFlag") for member in requested_members)
    lock = _lock_payload(album_id, child_id)
    current_username = _get_ext_user_nickname() or ctx.get("username") or None
    lock_user = str((lock or {}).get("username") or "")
    current_holds_lock = bool(lock_user and current_username and lock_user == current_username)
    return {
        "mode": child.get("mode") or "normal",
        "lock": lock,
        "history": history,
        "members": members,
        "currentExternalUserId": current_id,
        "currentUserStatus": current,
        "workerName": current_username,
        "currentUserHoldsLock": current_holds_lock,
        "canUnlock": bool(current_holds_lock or ctx["can_manage"]),
        "canForceUnlock": bool(ctx["is_admin"]),
        "completed": completed,
    }


_CAPTURE_CACHE_NAME = ".capture-times.json"
_EXIF_DATETIME_TAGS = (36867, 36868, 306)  # DateTimeOriginal, DateTimeDigitized, DateTime


def _natural_name_key(value: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value))


def _parse_exif_datetime(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    text = str(value or "").strip(" \x00")
    for pattern in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], pattern).isoformat()
        except ValueError:
            continue
    return None


def _image_capture_times(base: Path, paths: list[Path]) -> dict[str, str | None]:
    """Read EXIF once and retain the result in the server-side media cache."""
    cache_dir = Path(current_app.config.get("ALBUM_CAPTURE_CACHE_DIR") or "/mnt/mfu/tmp/album_capture_times")
    cache_key = hashlib.sha256(str(base.resolve()).encode("utf-8")).hexdigest()
    cache_path = cache_dir / f"{cache_key}-{_CAPTURE_CACHE_NAME}"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.is_file() else {}
    except (OSError, ValueError, TypeError):
        cached = {}

    changed = False
    result: dict[str, str | None] = {}
    live_names = {path.name for path in paths}
    for name in list(cached):
        if name not in live_names:
            cached.pop(name, None)
            changed = True

    for path in paths:
        try:
            stat = path.stat()
            signature = f"{stat.st_size}:{stat.st_mtime_ns}"
        except OSError:
            continue
        entry = cached.get(path.name)
        if isinstance(entry, dict) and entry.get("signature") == signature:
            result[path.name] = entry.get("capturedAt") or None
            continue
        captured_at = None
        try:
            with Image.open(path) as image:
                exif = image.getexif()
                exif_sets = [exif]
                try:
                    exif_sets.insert(0, exif.get_ifd(34665))  # ExifIFD
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass
                for values in exif_sets:
                    for tag in _EXIF_DATETIME_TAGS:
                        captured_at = _parse_exif_datetime(values.get(tag))
                        if captured_at:
                            break
                    if captured_at:
                        break
        except (OSError, ValueError, TypeError):
            pass
        cached[path.name] = {"signature": signature, "capturedAt": captured_at}
        result[path.name] = captured_at
        changed = True

    if changed:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(cached, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            temporary.replace(cache_path)
        except OSError:
            current_app.logger.warning("album capture-time cache could not be written: %s", cache_path)
    return result


def _media_rows(ctx: dict, child: dict, *, include_capture_times: bool = False) -> list[dict]:
    album_id = ctx["album_id"]
    child_id = str(child["folder"])
    mode = str(child.get("mode") or "normal")
    rows: list[dict] = []

    if mode == "movie":
        base = Path(storage_child_dir(album_id, child_id, "movie"))
        original = base / "original"
        encoded = base / "encoded"
        if original.is_dir():
            for path in original.iterdir():
                if not path.is_file() or not allowed_movie(path.name) or path.name.endswith(".web.mp4"):
                    continue
                stem = path.stem
                web = encoded / f"{stem}.web.mp4"
                poster = next(
                    (candidate for candidate in (encoded / f"{stem}.poster.jpg", original / f"{stem}.poster.jpg") if candidate.is_file()),
                    None,
                )
                rows.append({
                    "id": path.name,
                    "name": path.name,
                    "kind": "video",
                    "size": path.stat().st_size,
                    "modifiedAt": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                    "converting": not web.is_file(),
                    "viewUrl": url_for("album.movie_raw", album_id=album_id, child_id=child_id, filename=web.name if web.is_file() else path.name),
                    "downloadUrl": url_for("album.movie_download", album_id=album_id, child_id=child_id, filename=path.name),
                    "posterUrl": url_for("album.movie_poster", album_id=album_id, child_id=child_id, filename=poster.name) if poster else None,
                })
    else:
        base = Path(storage_child_dir(album_id, child_id, mode))
        image_paths: list[Path] = []
        if base.is_dir():
            for path in base.iterdir():
                if not path.is_file() or not allowed_file(path.name):
                    continue
                if mode == "process" and not path.name.startswith("latest."):
                    continue
                if mode != "process" and path.name.startswith("latest."):
                    continue
                image_paths.append(path)
                rows.append({
                    "id": path.name,
                    "name": path.name,
                    "kind": "image",
                    "size": path.stat().st_size,
                    "modifiedAt": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
                    "viewUrl": url_for("album.image", album_id=album_id, child_id=child_id, filename=path.name),
                    "downloadUrl": url_for("album.image", album_id=album_id, child_id=child_id, filename=path.name, download=1),
                    "thumbnailUrl": resolve_thumb_url(album_id, child_id, path.name),
                })
        if include_capture_times and image_paths:
            capture_times = _image_capture_times(base, image_paths)
            for row in rows:
                row["capturedAt"] = capture_times.get(str(row.get("name") or ""))
                row["sortSource"] = "exif" if row["capturedAt"] else "filename"
    rows.sort(key=lambda row: _natural_name_key(str(row.get("name") or "")))
    return rows


def _paginate(rows: list[dict]) -> tuple[list[dict], dict]:
    search = str(request.args.get("search") or "").strip().casefold()
    if search:
        rows = [row for row in rows if search in str(row.get("name") or "").casefold()]
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = min(API_MAX_PAGE_SIZE, max(1, int(request.args.get("perPage", 100))))
    except (TypeError, ValueError):
        per_page = 100
    descending = str(request.args.get("sort") or "asc").lower() in {"desc", "captured_desc"}
    captured = [row for row in rows if row.get("capturedAt")]
    unnamed = [row for row in rows if not row.get("capturedAt")]
    captured.sort(
        key=lambda row: (str(row.get("capturedAt")), _natural_name_key(str(row.get("name") or ""))),
        reverse=descending,
    )
    unnamed.sort(key=lambda row: _natural_name_key(str(row.get("name") or "")), reverse=descending)
    rows = captured + unnamed
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    return rows[start:start + per_page], {
        "page": page,
        "perPage": per_page,
        "total": total,
        "pages": pages,
        "hasNext": page < pages,
        "hasPrevious": page > 1,
    }


def _child_payload(ctx: dict, child: dict) -> dict:
    rows = _media_rows(ctx, child)
    processing = _processing_payload(ctx, child) if child.get("mode") == "process" else None
    return {
        "id": child.get("folder"),
        "rowId": _json_value(child.get("id")),
        "name": child.get("name"),
        "mode": child.get("mode") or "normal",
        "createdAt": _json_value(child.get("created_at")),
        "updatedAt": _json_value(child.get("updated_at")),
        "mediaCount": len(rows),
        "mediaUnit": "本" if child.get("mode") == "movie" else "枚",
        "processing": processing,
        "permissions": _child_permissions(ctx, child),
        "urls": {
            "view": url_for("album.view_child", album_id=ctx["album_id"], child_id=child.get("folder")),
            "upload": url_for("album.upload_child", album_id=ctx["album_id"], child_id=child.get("folder")),
        },
    }


def _require_manage(ctx: dict):
    if not ctx["can_manage"]:
        return _error("forbidden", 403, "管理者または所有者の権限が必要です。")
    return None


def _valid_media_name(value: Any) -> str | None:
    name = str(value or "").strip()
    if not name or name in {".", ".."} or not _SAFE_FILENAME_RE.fullmatch(name):
        return None
    return name


@album_bp.get("/api/session")
def api_album_session():
    """Bootstrap same-origin mutation protection before album authentication."""
    return _ok(
        csrfToken=_csrf_token(),
        user=session.get("user"),
        externalUserId=_current_external_user_id(),
    )


@album_bp.get("/api/albums")
def api_albums():
    username = str(session.get("user") or "")
    ext_user_id = _current_external_user_id()
    if username == "admin":
        rows = db_get_all(
            "SELECT id, album_name, owner, event_id, access_mode FROM albums ORDER BY album_name ASC"
        )
    elif username:
        rows = db_get_all(
            """
            SELECT DISTINCT a.id, a.album_name, a.owner, a.event_id, a.access_mode
              FROM albums AS a
              LEFT JOIN mfu_event_admin_acl AS acl
                ON acl.event_id=a.event_id AND acl.username=%s
             WHERE a.owner=%s OR acl.id IS NOT NULL
             ORDER BY a.album_name ASC
            """,
            (username, username),
        )
    elif ext_user_id:
        rows = db_get_all(
            """
            SELECT DISTINCT a.id, a.album_name, a.owner, a.event_id, a.access_mode
              FROM albums AS a
              JOIN mfu_event_member AS m ON m.event_id=a.event_id
              JOIN external_login_user AS u ON u.id=m.user_id
             WHERE a.access_mode='event' AND m.user_id=%s
               AND m.status='approved' AND COALESCE(m.is_canceled,0)=0
               AND COALESCE(u.is_deleted,0)=0
             ORDER BY a.album_name ASC
            """,
            (ext_user_id,),
        )
    else:
        return _error("login_required", 401, "ログインが必要です。")
    return _ok(albums=[{key: _json_value(value) for key, value in row.items()} for row in rows])


@album_bp.post("/api/albums/<album_id>/authenticate")
def api_album_authenticate(album_id: str):
    ctx, failure = _album_context(album_id, require_view=False)
    if failure:
        return failure
    assert ctx is not None
    if ctx["is_admin"] or ctx["is_owner"]:
        _grant_album_auth(album_id)
        return _ok(role=ctx["role"], authenticated=True)
    if ctx["gate"].get("access_mode") == "event":
        event_id = ctx["gate"].get("event_id")
        if event_id and _is_event_member_approved(int(event_id)):
            _grant_album_auth(album_id)
            return _ok(role="event_member", authenticated=True)
        _revoke_album_auth(album_id)
        return _error(
            "event_album_auth_required",
            403,
            "イベント参加承認が必要です。",
            joinUrl=url_for("album.album_access", album_id=album_id),
        )
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token") or "")
    if token and token == str(ctx["meta"].get("access_token") or ""):
        _grant_album_auth(album_id)
        return _ok(role="token_viewer", authenticated=True)
    return _error("invalid_token", 403, "アクセストークンが正しくありません。")


@album_bp.get("/api/albums/<album_id>")
def api_album(album_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    assert ctx is not None
    event = _event_summary(ctx["gate"].get("event_id"))
    album = {
        "id": album_id,
        "name": ctx["meta"].get("album_name"),
        "owner": ctx["meta"].get("owner"),
        "accessMode": ctx["gate"].get("access_mode") or "token",
        "eventId": _json_value(ctx["gate"].get("event_id")),
        "event": event,
        "permissions": _permissions(ctx),
        "childrenUrl": url_for("album.api_album_children", album_id=album_id),
        "accessUrl": url_for("album.album_access", album_id=album_id),
    }
    if ctx["can_manage"] and ctx["gate"].get("access_mode") == "token":
        album["accessToken"] = ctx["meta"].get("access_token")
    return _ok(album=album)


@album_bp.patch("/api/albums/<album_id>")
def api_album_rename(album_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    guard = _require_manage(ctx)
    if guard:
        return guard
    if ctx["gate"].get("access_mode") == "event":
        return _error("event_album_name_managed_by_event", 409, "イベント管理画面から変更してください。")
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return _error("name_required", 400)
    db_exec("UPDATE albums SET album_name=%s WHERE id=%s", (name, album_id))
    return _ok(album={"id": album_id, "name": name})


@album_bp.delete("/api/albums/<album_id>")
def api_album_delete(album_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    guard = _require_manage(ctx)
    if guard:
        return guard
    passkey = require_admin_passkey(f"album_delete:{album_id}")
    if passkey:
        return passkey
    for root in (ALBUM_ROOT, MOVIE_ROOT):
        shutil.rmtree(os.path.join(root, album_id), ignore_errors=True)
    delete_album_row(album_id)
    _revoke_album_auth(album_id)
    return _ok(deleted=True, albumId=album_id)


@album_bp.get("/api/albums/<album_id>/children")
def api_album_children(album_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    children = [_child_payload(ctx, child) for child in ctx["meta"].get("children", [])]
    return _ok(children=children, permissions=_permissions(ctx))


@album_bp.post("/api/albums/<album_id>/children")
def api_album_child_create(album_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    if not ctx["can_create_child"]:
        return _error("forbidden", 403, "子アルバムを作成する権限がありません。")
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    mode = str(payload.get("mode") or "normal").lower()
    if not name:
        return _error("name_required", 400)
    if mode not in {"normal", "process", "movie"}:
        return _error("invalid_mode", 400)
    if not _validate_child_template(ctx, name, mode):
        return _error("invalid_child_template", 400, "名前テンプレートを選択してください。種類はテンプレートにより固定されます。")
    if any(str(item.get("name")) == name for item in ctx["meta"].get("children", [])):
        return _error("child_name_exists", 409)
    creator_id = ctx.get("current_ext_user_id") if ctx.get("event_member") else None
    child_id = add_child_row(album_id, name, mode, created_by_ext_user_id=creator_id)
    os.makedirs(storage_child_dir(album_id, child_id, mode), exist_ok=True)
    refreshed, _ = _album_context(album_id)
    child = _child(refreshed, child_id)
    _audit_child_action("create", refreshed, child, name=name, mode=mode)
    return _ok(child=_child_payload(refreshed, child)), 201


@album_bp.patch("/api/albums/<album_id>/children/<child_id>")
def api_album_child_rename(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    child = _child(ctx, child_id)
    if not child:
        return _error("child_not_found", 404)
    guard = _require_child_permission(ctx, child, "canRenameChild")
    if guard:
        return guard
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        return _error("name_required", 400)
    db_exec("UPDATE album_children SET name=%s WHERE album_id=%s AND folder=%s", (name, album_id, child_id))
    _audit_child_action("rename", ctx, child, before=child.get("name"), after=name)
    child = {**child, "name": name}
    return _ok(child=_child_payload(ctx, child))


@album_bp.delete("/api/albums/<album_id>/children/<child_id>")
def api_album_child_delete(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    child = _child(ctx, child_id)
    if not child:
        return _error("child_not_found", 404)
    guard = _require_child_permission(ctx, child, "canDeleteChild")
    if guard:
        return guard
    if ctx["is_admin"]:
        passkey = require_admin_passkey(f"album_child_delete:{album_id}:{child_id}")
        if passkey:
            return passkey
    try:
        release_lock_db(album_id, child_id, username=None, force=True)
    except Exception:
        pass
    shutil.rmtree(storage_child_dir(album_id, child_id, child.get("mode")), ignore_errors=True)
    delete_child_row(album_id, child_id)
    _audit_child_action("delete", ctx, child, name=child.get("name"))
    return _ok(deleted=True, childId=child_id)


@album_bp.get("/api/albums/<album_id>/children/<child_id>/media")
def api_album_media(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    child = _child(ctx, child_id)
    if not child:
        return _error("child_not_found", 404)
    rows, pagination = _paginate(_media_rows(ctx, child, include_capture_times=True))
    return _ok(
        child=_child_payload(ctx, child),
        media=rows,
        pagination=pagination,
        permissions=_child_permissions(ctx, child),
    )


@album_bp.post("/api/albums/<album_id>/children/<child_id>/media")
def api_album_media_upload(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    child = _child(ctx, child_id)
    if not child:
        return _error("child_not_found", 404)
    guard = _require_child_permission(ctx, child, "canUpload")
    if guard:
        return guard
    if child.get("mode") == "process" and _media_rows(ctx, child):
        processing = _processing_payload(ctx, child)
        if not processing.get("currentUserHoldsLock"):
            return _error("processing_lock_required", 409, "加工済み画像のアップロードには加工ロックが必要です。")
    if not request.files.getlist("file"):
        return _error("files_required", 400)

    # Reuse the established upload workflow. It preserves numbering, process
    # history, movie encoding queue and all current e-mail/push notifications.
    response = upload_child(album_id, child_id)
    status = getattr(response, "status_code", 200)
    if status >= 400:
        return response
    rows = _media_rows(ctx, child)
    _audit_child_action("upload", ctx, child, media_count=len(rows))
    return _ok(uploaded=True, mediaCount=len(rows), media=rows[-20:])


def _media_path(child: dict, album_id: str, child_id: str, name: str) -> Path | None:
    mode = str(child.get("mode") or "normal")
    if mode == "movie":
        path = Path(storage_child_dir(album_id, child_id, "movie")) / "original" / name
    else:
        path = Path(storage_child_dir(album_id, child_id, mode)) / name
    return path if path.is_file() else None


@album_bp.patch("/api/albums/<album_id>/children/<child_id>/media/<path:filename>")
def api_album_media_rename(album_id: str, child_id: str, filename: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    guard = _require_manage(ctx)
    if guard:
        return guard
    child = _child(ctx, child_id)
    old_name = _valid_media_name(filename)
    if not child or not old_name:
        return _error("media_not_found", 404)
    if child.get("mode") == "process":
        return _error("process_media_rename_not_allowed", 409, "加工用のlatestファイルは名前変更できません。")
    old_path = _media_path(child, album_id, child_id, old_name)
    if not old_path:
        return _error("media_not_found", 404)
    payload = request.get_json(silent=True) or {}
    requested = _valid_media_name(payload.get("name"))
    if not requested:
        return _error("name_required", 400)
    old_ext = old_path.suffix.lower()
    new_path_value = Path(requested)
    new_name = requested if new_path_value.suffix else f"{requested}{old_ext}"
    if Path(new_name).suffix.lower() != old_ext:
        return _error("extension_change_not_allowed", 400)
    target = old_path.with_name(new_name)
    if target.exists():
        return _error("media_name_exists", 409)
    old_stem = old_path.stem
    new_stem = target.stem
    old_path.rename(target)

    mode = str(child.get("mode") or "normal")
    if mode == "movie":
        base = Path(storage_child_dir(album_id, child_id, "movie"))
        for suffix in (".web.mp4", ".poster.jpg"):
            for parent in (base / "encoded", base / "original"):
                sidecar = parent / f"{old_stem}{suffix}"
                if sidecar.is_file():
                    sidecar.rename(parent / f"{new_stem}{suffix}")
    else:
        thumbs = Path(ALBUM_ROOT) / album_id / child_id / "thumbs"
        for ext in (".webp", ".jpg", ".jpeg"):
            sidecar = thumbs / f"{old_stem}{ext}"
            if sidecar.is_file():
                sidecar.rename(thumbs / f"{new_stem}{ext}")
    return _ok(renamed=True, oldName=old_name, name=new_name)


@album_bp.delete("/api/albums/<album_id>/children/<child_id>/media")
def api_album_media_delete(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    child = _child(ctx, child_id)
    if not child:
        return _error("child_not_found", 404)
    guard = _require_child_permission(ctx, child, "canDeleteMedia")
    if guard:
        return guard
    if ctx["is_admin"]:
        passkey = require_admin_passkey(f"album_media_delete:{album_id}:{child_id}")
        if passkey:
            return passkey
    payload = request.get_json(silent=True) or {}
    names = payload.get("names") or []
    if not isinstance(names, list) or not names:
        return _error("names_required", 400)
    deleted, missing = [], []
    for value in names[:500]:
        name = _valid_media_name(value)
        path = _media_path(child, album_id, child_id, name) if name else None
        if not path:
            missing.append(str(value))
            continue
        stem = path.stem
        path.unlink()
        deleted.append(name)
        if child.get("mode") == "movie":
            base = Path(storage_child_dir(album_id, child_id, "movie"))
            for suffix in (".web.mp4", ".poster.jpg"):
                for parent in (base / "encoded", base / "original"):
                    (parent / f"{stem}{suffix}").unlink(missing_ok=True)
        else:
            thumbs = Path(ALBUM_ROOT) / album_id / child_id / "thumbs"
            for ext in (".webp", ".jpg", ".jpeg"):
                (thumbs / f"{stem}{ext}").unlink(missing_ok=True)
    if child.get("mode") != "movie" and deleted:
        try:
            enqueue_thumb_job("album", album_id, child_id)
        except Exception:
            pass
    if deleted:
        _audit_child_action("delete_media", ctx, child, names=deleted)
    return _ok(deleted=deleted, missing=missing)


@album_bp.get("/api/albums/<album_id>/children/<child_id>/processing")
def api_album_processing(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    child = _child(ctx, child_id)
    if not child:
        return _error("child_not_found", 404)
    return _ok(processing=_processing_payload(ctx, child), permissions=_permissions(ctx))


@album_bp.post("/api/albums/<album_id>/children/<child_id>/processing/begin")
def api_album_processing_begin(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    child = _child(ctx, child_id)
    if not child or child.get("mode") != "process":
        return _error("process_child_required", 400, "加工用の子アルバムではありません。")
    filename = find_latest_filename(album_id, child_id)
    if not filename:
        return _error("latest_media_not_found", 404, "加工用画像がありません。")
    username = str(_get_ext_user_nickname() or ctx.get("username") or "").strip() or "不明"
    ok, message = try_acquire_lock_db(album_id, child_id, username, ttl_sec=LOCK_TTL_SEC)
    if not ok:
        return _error("processing_locked", 409, str(message))
    lock_path = Path(storage_child_dir(album_id, child_id, "process")) / "lock.json"
    try:
        lock_path.write_text(
            json.dumps({"user": username, "timestamp": int(time.time())}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        current_app.logger.warning("processing lock sidecar could not be written: %s", lock_path)
    return _ok(
        message=f"{username} さんとして加工を開始しました。",
        downloadUrl=url_for(
            "album.api_album_processing_latest",
            album_id=album_id,
            child_id=child_id,
        ),
        processing=_processing_payload(ctx, child),
    )


@album_bp.get("/api/albums/<album_id>/children/<child_id>/processing/latest")
def api_album_processing_latest(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    child = _child(ctx, child_id)
    if not child or child.get("mode") != "process":
        return _error("process_child_required", 400)
    processing = _processing_payload(ctx, child)
    if not (processing.get("currentUserHoldsLock") or ctx["can_manage"]):
        return _error("processing_lock_required", 409, "先に加工ロックを取得してください。")
    filename = find_latest_filename(album_id, child_id)
    path = _media_path(child, album_id, child_id, filename) if filename else None
    if not path:
        return _error("latest_media_not_found", 404, "加工用画像がありません。")
    return send_file(path, as_attachment=True, download_name=path.name, conditional=True)


def _remove_processing_lock(album_id: str, child_id: str, child: dict, *, force: bool, username: str | None) -> None:
    release_lock_db(album_id, child_id, username=username, force=force)
    lock_path = Path(storage_child_dir(album_id, child_id, child.get("mode"))) / "lock.json"
    lock_path.unlink(missing_ok=True)


@album_bp.post("/api/albums/<album_id>/children/<child_id>/processing/unlock")
def api_album_processing_unlock(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    child = _child(ctx, child_id)
    if not child or child.get("mode") != "process":
        return _error("process_child_required", 400)
    processing = _processing_payload(ctx, child)
    if not processing.get("canUnlock"):
        return _error("forbidden", 403, "この加工ロックを解除できません。")
    username = str(processing.get("workerName") or "").strip() or None
    _remove_processing_lock(album_id, child_id, child, force=bool(ctx["can_manage"]), username=username)
    return _ok(unlocked=True, processing=_processing_payload(ctx, child))


@album_bp.post("/api/albums/<album_id>/children/<child_id>/processing/force-unlock")
def api_album_processing_force_unlock(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    if not ctx["is_admin"]:
        return _error("forbidden", 403)
    child = _child(ctx, child_id)
    if not child or child.get("mode") != "process":
        return _error("process_child_required", 400)
    _remove_processing_lock(album_id, child_id, child, force=True, username=None)
    return _ok(unlocked=True, processing=_processing_payload(ctx, child))


@album_bp.put("/api/albums/<album_id>/children/<child_id>/processing/requests")
def api_album_processing_requests(album_id: str, child_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    if not ctx["can_manage_processing"]:
        return _error("forbidden", 403)
    child = _child(ctx, child_id)
    if not child or child.get("mode") != "process":
        return _error("process_child_required", 400)
    return request_process(album_id, child_id)


@album_bp.put("/api/albums/<album_id>/children/<child_id>/processing/members/<int:ext_user_id>")
def api_album_processing_member(album_id: str, child_id: str, ext_user_id: int):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    if not ctx["can_manage_processing"]:
        return _error("forbidden", 403)
    child = _child(ctx, child_id)
    if not child or child.get("mode") != "process":
        return _error("process_child_required", 400)
    event_id = ctx["gate"].get("event_id")
    active_member_ids = {
        int(member.get("user_id"))
        for member in (_fetch_event_process_members(int(event_id)) if event_id else [])
        if member.get("user_id") is not None
    }
    if ext_user_id not in active_member_ids:
        return _error("member_not_found", 404, "承認済みの加工対象者が見つかりません。")
    current_ext_user_id = _current_external_user_id()
    if not ctx["can_manage"] and current_ext_user_id != ext_user_id:
        return _error("forbidden", 403, "参加者は自分の加工状態だけを変更できます。")
    payload = dict(request.get_json(silent=True) or {})
    if payload.get("complete_flag") and current_ext_user_id == ext_user_id and not ctx["can_manage"]:
        processing = _processing_payload(ctx, child)
        if not processing.get("currentUserHoldsLock"):
            return _error("processing_lock_required", 409, "加工完了には加工ロックが必要です。")
    payload["ext_user_id"] = ext_user_id
    # Reuse the existing notification/lock workflow with a normalized payload.
    return update_process_status(album_id, child_id, payload)


def _download_entries(ctx: dict, child_id: str | None, names: list[str] | None) -> list[tuple[str, str]]:
    selected = {str(name) for name in (names or [])}
    entries: list[tuple[str, str]] = []
    children = ctx["meta"].get("children", [])
    if child_id:
        children = [child for child in children if str(child.get("folder")) == child_id]
    for child in children:
        child_name = str(child.get("name") or child.get("folder") or "album")
        for media in _media_rows(ctx, child):
            name = str(media["name"])
            if selected and name not in selected:
                continue
            path = _media_path(child, ctx["album_id"], str(child["folder"]), name)
            if path:
                entries.append((f"{child_name}/{name}", str(path)))
    return entries


@album_bp.post("/api/albums/<album_id>/download-jobs")
def api_album_download_job_create(album_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    payload = request.get_json(silent=True) or {}
    child_id = str(payload.get("childId") or "").strip() or None
    names = payload.get("names") or []
    if names and not isinstance(names, list):
        return _error("invalid_names", 400)
    if child_id and not _child(ctx, child_id):
        return _error("child_not_found", 404)
    entries = _download_entries(ctx, child_id, names)
    if not entries:
        return _error("no_media", 404, "対象ファイルがありません。")
    key = f"album-{uuid.uuid4().hex}"
    child = _child(ctx, child_id) if child_id else None
    label = str((child or {}).get("name") or ctx["meta"].get("album_name") or "album")
    start_zip_entries_job(
        entries,
        key=key,
        download_name=f"{label}.zip",
        access={"type": "album", "album_ids": [album_id]},
    )
    return _ok(
        job={
            "id": key,
            "status": "queued",
            "progressUrl": url_for("album.api_album_download_job", album_id=album_id, job_id=key),
            "downloadUrl": f"/api/zip-download/{key}",
        }
    ), 202


@album_bp.get("/api/albums/<album_id>/download-jobs/<job_id>")
def api_album_download_job(album_id: str, job_id: str):
    ctx, failure = _album_context(album_id)
    if failure:
        return failure
    progress = read_zip_progress(job_id)
    access = (progress or {}).get("access") or {}
    if not progress or access.get("type") != "album" or album_id not in (access.get("album_ids") or []):
        return _error("job_not_found", 404)
    return _ok(
        job={
            **{key: _json_value(value) for key, value in progress.items() if key != "access"},
            "id": job_id,
            "downloadUrl": f"/api/zip-download/{job_id}" if progress.get("status") == "done" else None,
        }
    )
