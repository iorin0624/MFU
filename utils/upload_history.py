from pathlib import Path
import shutil
import uuid as uuidlib
from datetime import date, datetime

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, send_file, session, url_for
from werkzeug.utils import secure_filename

from app.utils.db import get_db
from app.utils.layer_reply_store import (
    delete_layer_replies,
    get_layer_reply_summary,
    layer_reply_file_exists,
    list_layer_reply_groups,
)
from app.utils.upload_deletion import delete_normal_upload
from app.utils.admin_passkey_stepup import require_admin_passkey
from app.utils.zip_stream import start_zip_entries_job

upload_history_bp = Blueprint("upload_history", __name__)

UPLOAD_BASE_DIR = "/mnt/mfu/uploads"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _storage_root() -> Path:
    return Path(current_app.config.get("STORAGE_ROOT", UPLOAD_BASE_DIR)).resolve()


def _layer_root() -> Path:
    return (_storage_root() / "layer_uploads").resolve()


def _is_admin(username: str) -> bool:
    return username == "admin"


def _fetch_uploads_for_user(username: str, *, scope: str):
    active_column = {
        "upload": "upload_deleted_at",
        "layer": "layer_deleted_at",
    }.get(scope)
    if not active_column:
        raise ValueError("invalid upload history scope")
    db = get_db()
    cursor = db.cursor(dictionary=True)
    if _is_admin(username):
        cursor.execute(
            f"SELECT * FROM uploads WHERE {active_column} IS NULL ORDER BY created_at DESC"
        )
    else:
        cursor.execute(
            f"SELECT * FROM uploads WHERE username = %s AND {active_column} IS NULL ORDER BY created_at DESC",
            (username,),
        )
    uploads = cursor.fetchall()
    db.close()
    return uploads


def _fetch_unified_uploads_for_user(username: str):
    """Return upload rows that still have a normal or layer-side scope."""

    db = get_db()
    cursor = db.cursor(dictionary=True)
    owner_filter = "" if _is_admin(username) else "AND upload.username = %s"
    params = () if _is_admin(username) else (username,)
    cursor.execute(
        f"""
        SELECT upload.*,
               COALESCE(mode.enable_layer_upload_url, 0) AS reply_enabled,
               (SELECT COUNT(*) FROM files WHERE files.upload_id=upload.id) AS file_count
          FROM uploads AS upload
          LEFT JOIN upload_modes AS mode
            ON mode.username = upload.username
           AND mode.mode = upload.mode
         WHERE (upload.upload_deleted_at IS NULL OR upload.layer_deleted_at IS NULL)
               {owner_filter}
         ORDER BY upload.created_at DESC, upload.id DESC
        """,
        params,
    )
    uploads = cursor.fetchall()
    db.close()
    return uploads


def _protection_label(upload: dict) -> str:
    method = str(upload.get("auth_method") or "").strip().lower()
    if method == "password" or (
        not method and (upload.get("password_hash") or upload.get("password"))
    ):
        return "PW"
    if method == "access_token":
        return "アクセストークン"
    if method == "email_otp":
        return "メールOTP認証"
    return "なし"


def _format_japanese_date(value) -> str:
    if value in (None, ""):
        return "－"
    parsed = value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value[:10])
        except ValueError:
            return value
    if isinstance(parsed, (date, datetime)):
        return f"{parsed.year}年{parsed.month}月{parsed.day}日"
    return str(value)


def _format_japanese_datetime(value) -> str:
    if value in (None, ""):
        return "－"
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(parsed, datetime):
        return f"{parsed.year}年{parsed.month}月{parsed.day}日 {parsed.hour}:{parsed.minute:02d}"
    return _format_japanese_date(parsed)


def _format_expire_at(value) -> str:
    if isinstance(value, str) and len(value.strip()) == 10:
        return f"{_format_japanese_date(value)} 23:59"
    if isinstance(value, date) and not isinstance(value, datetime):
        return f"{_format_japanese_date(value)} 23:59"
    return _format_japanese_datetime(value)


def _build_unified_upload_groups(uploads: list[dict]) -> tuple[list[dict], list[dict]]:
    normal_uploads: list[dict] = []
    reply_only_uploads: list[dict] = []
    for upload in uploads:
        row = {**upload, **_layer_summary(upload["id"])}
        row["protection_label"] = _protection_label(row)
        row["normal_active"] = row.get("upload_deleted_at") is None
        row["layer_active"] = row.get("layer_deleted_at") is None
        row["reply_enabled"] = bool(row.get("reply_enabled")) and row["layer_active"]
        row["has_reply_scope"] = row["layer_active"] and (
            row["reply_enabled"] or row["has_layer_upload"]
        )
        row["date_display"] = _format_japanese_date(row.get("date"))
        row["expire_at_display"] = _format_expire_at(row.get("expire_at"))
        row["created_at_display"] = _format_japanese_datetime(row.get("created_at"))
        if row["normal_active"]:
            normal_uploads.append(row)
        elif row["has_reply_scope"]:
            reply_only_uploads.append(row)
    return normal_uploads, reply_only_uploads


def _fetch_upload_by_uuid(uuid: str, *, scope: str | None = None):
    active_column = {
        "upload": "upload_deleted_at",
        "layer": "layer_deleted_at",
        None: None,
    }.get(scope)
    if scope is not None and not active_column:
        raise ValueError("invalid upload history scope")
    db = get_db()
    cursor = db.cursor(dictionary=True)
    where_active = f" AND {active_column} IS NULL" if active_column else ""
    cursor.execute(f"SELECT * FROM uploads WHERE uuid = %s{where_active}", (uuid,))
    upload = cursor.fetchone()
    db.close()
    return upload


def _ensure_upload_permission(upload: dict, username: str):
    if not upload:
        abort(404)
    if not _is_admin(username) and upload.get("username") != username:
        abort(403)


def _safe_path(base: Path, *parts: str):
    cleaned_parts = []
    for part in parts:
        candidate = (part or "").strip()
        if not candidate or Path(candidate).name != candidate:
            return None
        cleaned_parts.append(candidate)
    if not cleaned_parts:
        return None
    target = base.joinpath(*cleaned_parts).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


def _list_layer_groups(upload_id: int):
    return list_layer_reply_groups(upload_id)


def _layer_summary(upload_id: int):
    return get_layer_reply_summary(upload_id)


@upload_history_bp.route("/upload_list")
def upload_list():
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    uploads = _fetch_unified_uploads_for_user(username)
    normal_uploads, reply_only_uploads = _build_unified_upload_groups(uploads)

    return render_template(
        "upload_list.html",
        normal_uploads=normal_uploads,
        reply_only_uploads=reply_only_uploads,
        is_admin=_is_admin(username),
    )


@upload_history_bp.route("/layer_upload_list")
def layer_upload_list():
    if "user" not in session:
        return redirect(url_for("login"))

    return redirect(url_for("upload_history.upload_list", group="reply-only") + "#reply-only")


@upload_history_bp.route("/layer_upload_list/<uuid>")
def layer_upload_detail(uuid):
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    upload = _fetch_upload_by_uuid(uuid, scope="layer")
    _ensure_upload_permission(upload, username)

    groups = _list_layer_groups(upload["id"])
    return render_template("layer_upload_detail.html", upload=upload, groups=groups)


def _layer_zip_entries(uuid: str, groups: list[dict]):
    entries = []
    layer_base = (_layer_root() / secure_filename(uuid)).resolve()
    for group in groups:
        reply_uuid = str(group.get("reply_uuid") or "").strip()
        folder_name = str(group.get("folder_name") or reply_uuid).strip()
        original_dir = (layer_base / secure_filename(folder_name) / "original").resolve()
        try:
            original_dir.relative_to(layer_base)
        except ValueError:
            continue
        for filename in group.get("images") or []:
            target = _safe_path(original_dir, filename)
            if not target or not target.is_file() or target.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            entries.append((f"{reply_uuid}/{filename}", str(target)))
    return entries


@upload_history_bp.post("/layer_upload_list/<uuid>/zip/prepare")
def layer_upload_zip_prepare(uuid):
    if "user" not in session:
        return jsonify(ok=False, error="login_required"), 401

    username = session["user"]
    upload = _fetch_upload_by_uuid(uuid, scope="layer")
    _ensure_upload_permission(upload, username)
    groups = _list_layer_groups(upload["id"])
    payload = request.get_json(silent=True) or {}
    requested_reply_uuid = str(payload.get("reply_uuid") or "").strip()
    selected_groups = groups
    if requested_reply_uuid:
        selected_groups = [
            group for group in groups
            if str(group.get("reply_uuid") or "") == requested_reply_uuid
        ]
        if not selected_groups:
            return jsonify(ok=False, error="対象のアップロードが見つかりません。"), 404

    entries = _layer_zip_entries(upload["uuid"], selected_groups)
    if not entries:
        return jsonify(ok=False, error="対象画像がありません。"), 404

    key = f"layer-{uuidlib.uuid4().hex}"
    try:
        start_zip_entries_job(
            entries,
            key=key,
            download_name=f"{requested_reply_uuid or upload['uuid']}.zip",
            access={
                "type": "layer_upload",
                "upload_uuid": upload["uuid"],
                "username": upload["username"],
                "reply_uuid": requested_reply_uuid,
            },
        )
    except FileExistsError:
        return jsonify(ok=False, error="already_in_progress"), 409

    return jsonify(
        ok=True,
        key=key,
        file_count=len(entries),
        reply_uuid=requested_reply_uuid,
        progress_url=f"/api/zip-progress?key={key}",
        download_url=f"/api/zip-download/{key}",
    ), 202


@upload_history_bp.route("/layer_upload_list/<uuid>/image/<reply_uuid>/<filename>")
def layer_upload_image(uuid, reply_uuid, filename):
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    upload = _fetch_upload_by_uuid(uuid, scope="layer")
    _ensure_upload_permission(upload, username)

    if not layer_reply_file_exists(
        upload_id=upload["id"],
        reply_uuid=reply_uuid,
        filename=filename,
    ):
        abort(404)

    base = (_layer_root() / secure_filename(uuid) / secure_filename(reply_uuid) / "original").resolve()
    target = _safe_path(base, filename)
    if not target or not target.exists() or not target.is_file():
        abort(404)
    if target.suffix.lower() not in IMAGE_EXTENSIONS:
        abort(404)

    return send_file(target, conditional=True)


@upload_history_bp.route("/upload_delete/<uuid>", methods=["POST"])
def upload_delete(uuid):
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    db = get_db()
    cursor = db.cursor(dictionary=True)

    # admin以外は本人のアップロードのみ削除可
    cursor.execute(
        "SELECT * FROM uploads WHERE uuid = %s AND upload_deleted_at IS NULL",
        (uuid,),
    )
    upload = cursor.fetchone()

    if not upload:
        db.close()
        return abort(404)

    if not _is_admin(username) and upload["username"] != username:
        db.close()
        return abort(403)

    guard = require_admin_passkey(f"upload_delete:{uuid}")
    if guard:
        db.close()
        return guard

    db.close()

    delete_normal_upload(
        upload_id=upload["id"],
        uuid=uuid,
        storage_root=_storage_root(),
    )

    return redirect(url_for("upload_history.upload_list"))


@upload_history_bp.route("/layer_upload_delete/<uuid>", methods=["POST"])
def layer_upload_delete(uuid):
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute(
        "SELECT * FROM uploads WHERE uuid=%s AND layer_deleted_at IS NULL",
        (uuid,),
    )
    upload = cursor.fetchone()
    if not upload:
        db.close()
        return abort(404)
    if not _is_admin(username) and upload["username"] != username:
        db.close()
        return abort(403)

    guard = require_admin_passkey(f"layer_upload_delete:{uuid}")
    if guard:
        db.close()
        return guard

    try:
        delete_layer_replies(upload["id"], db=db, cursor=cursor)
        cursor.execute(
            "UPDATE uploads SET layer_deleted_at=COALESCE(layer_deleted_at, NOW()) WHERE id=%s",
            (upload["id"],),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    layer_dir = _layer_root() / secure_filename(uuid)
    if layer_dir.exists() and layer_dir.is_dir():
        shutil.rmtree(layer_dir)

    return redirect(url_for("upload_history.upload_list", group="reply-only") + "#reply-only")


@upload_history_bp.route("/upload_delete_all/<uuid>", methods=["POST"])
def upload_delete_all(uuid):
    """Delete both file scopes while preserving the parent UUID ledger row."""

    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM uploads WHERE uuid=%s", (uuid,))
    upload = cursor.fetchone()
    db.close()
    if not upload:
        return abort(404)
    if not _is_admin(username) and upload["username"] != username:
        return abort(403)

    guard = require_admin_passkey(f"upload_delete_all:{uuid}")
    if guard:
        return guard

    if upload.get("upload_deleted_at") is None:
        delete_normal_upload(
            upload_id=upload["id"],
            uuid=uuid,
            storage_root=_storage_root(),
        )

    if upload.get("layer_deleted_at") is None:
        db = get_db()
        cursor = db.cursor()
        try:
            delete_layer_replies(upload["id"], db=db, cursor=cursor)
            cursor.execute(
                "UPDATE uploads SET layer_deleted_at=COALESCE(layer_deleted_at, NOW()) WHERE id=%s",
                (upload["id"],),
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        layer_dir = _layer_root() / secure_filename(uuid)
        if layer_dir.exists() and layer_dir.is_dir():
            shutil.rmtree(layer_dir)

    return redirect(url_for("upload_history.upload_list"))
