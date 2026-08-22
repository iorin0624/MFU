from pathlib import Path
import shutil

from flask import Blueprint, abort, current_app, redirect, render_template, send_file, session, url_for
from werkzeug.utils import secure_filename

from app.utils.db import get_db
from app.utils.upload_deletion import delete_normal_upload

upload_history_bp = Blueprint("upload_history", __name__)

UPLOAD_BASE_DIR = "/mnt/mfu/uploads"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
DOWNLOAD_EXTENSIONS = {".zip", ".7z", ".rar"}


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


def _list_layer_groups(uuid: str):
    layer_root = _layer_root()
    parent_dir = (layer_root / secure_filename(uuid)).resolve()
    try:
        parent_dir.relative_to(layer_root)
    except ValueError:
        return []

    if not parent_dir.exists() or not parent_dir.is_dir():
        return []

    groups = []
    for folder in sorted(parent_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not folder.is_dir():
            continue
        original_dir = folder / "original"
        zip_dir = folder / "zip"

        images = []
        if original_dir.exists() and original_dir.is_dir():
            images = sorted(
                [
                    entry.name
                    for entry in original_dir.iterdir()
                    if entry.is_file() and entry.suffix.lower() in IMAGE_EXTENSIONS
                ]
            )

        zips = []
        if zip_dir.exists() and zip_dir.is_dir():
            zips = sorted(
                [
                    entry.name
                    for entry in zip_dir.iterdir()
                    if entry.is_file() and entry.suffix.lower() in DOWNLOAD_EXTENSIONS
                ]
            )

        groups.append(
            {
                "folder_name": folder.name,
                "images": images,
                "zips": zips,
                "updated_at": folder.stat().st_mtime,
            }
        )

    return groups


def _layer_summary(uuid: str):
    groups = _list_layer_groups(uuid)
    folder_count = len(groups)
    return {
        "has_layer_upload": folder_count > 0,
        "folder_count": folder_count,
        "has_zip": any(group["zips"] for group in groups),
        "latest_mtime": max((group["updated_at"] for group in groups), default=0),
    }


@upload_history_bp.route("/upload_list")
def upload_list():
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    uploads = _fetch_uploads_for_user(username, scope="upload")

    return render_template("upload_list.html", uploads=uploads, is_admin=_is_admin(username))


@upload_history_bp.route("/layer_upload_list")
def layer_upload_list():
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    uploads = _fetch_uploads_for_user(username, scope="layer")

    rows = []
    for upload in uploads:
        rows.append({**upload, **_layer_summary(upload["uuid"])})

    return render_template("layer_upload_list.html", uploads=rows)


@upload_history_bp.route("/layer_upload_list/<uuid>")
def layer_upload_detail(uuid):
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    upload = _fetch_upload_by_uuid(uuid, scope="layer")
    _ensure_upload_permission(upload, username)

    groups = _list_layer_groups(uuid)
    groups.sort(key=lambda group: group["updated_at"], reverse=True)
    return render_template("layer_upload_detail.html", upload=upload, groups=groups)


@upload_history_bp.route("/layer_upload_list/<uuid>/image/<reply_uuid>/<filename>")
def layer_upload_image(uuid, reply_uuid, filename):
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    upload = _fetch_upload_by_uuid(uuid, scope="layer")
    _ensure_upload_permission(upload, username)

    base = (_layer_root() / secure_filename(uuid) / secure_filename(reply_uuid) / "original").resolve()
    target = _safe_path(base, filename)
    if not target or not target.exists() or not target.is_file():
        abort(404)
    if target.suffix.lower() not in IMAGE_EXTENSIONS:
        abort(404)

    return send_file(target, conditional=True)


@upload_history_bp.route("/layer_upload_list/<uuid>/zip/<reply_uuid>/<filename>")
def layer_upload_zip(uuid, reply_uuid, filename):
    if "user" not in session:
        return redirect(url_for("login"))

    username = session["user"]
    upload = _fetch_upload_by_uuid(uuid, scope="layer")
    _ensure_upload_permission(upload, username)

    base = (_layer_root() / secure_filename(uuid) / secure_filename(reply_uuid) / "zip").resolve()
    target = _safe_path(base, filename)
    if not target or not target.exists() or not target.is_file():
        abort(404)
    if target.suffix.lower() not in DOWNLOAD_EXTENSIONS:
        abort(404)

    return send_file(target, as_attachment=True, download_name=target.name, conditional=True)


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

    cursor.execute(
        "UPDATE uploads SET layer_deleted_at=COALESCE(layer_deleted_at, NOW()) WHERE id=%s",
        (upload["id"],),
    )
    db.commit()
    db.close()

    layer_dir = _layer_root() / secure_filename(uuid)
    if layer_dir.exists() and layer_dir.is_dir():
        shutil.rmtree(layer_dir)

    return redirect(url_for("upload_history.layer_upload_list"))
