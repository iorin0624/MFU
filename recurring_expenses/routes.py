from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image
from werkzeug.utils import secure_filename

from app.freee_api import services as freee_services

from . import recurring_expenses_bp
from .freee_sync import delete_registered_month, register_month
from .partners import create_or_find_partner
from .repository import (
    add_attachment,
    delete_attachment,
    delete_month_attachments,
    get_attachment,
    get_master,
    get_month_item,
    list_attachments,
    list_masters,
    list_month_items,
    save_master,
    set_master_active,
    update_month_item,
)


STORAGE_ROOT = Path("/mnt/mfu/secure/recurring_expenses")
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif"}
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
STATUSES = {"pending", "waiting_receipt", "excluded"}
MASTER_DRAFT_SESSION_KEY = "recurring_expenses_master_draft"
COMPLETED_MONTH_STATUSES = {"registered", "manual", "excluded", "registering"}


def _admin_required():
    if not session.get("user"):
        flash("ログインが必要です。", "warning")
        return redirect(url_for("login", next=request.url))
    if session.get("user") != "admin":
        abort(403)
    return None


@recurring_expenses_bp.before_request
def require_admin():
    return _admin_required()


def _csrf_token() -> str:
    token = session.get("recurring_expenses_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["recurring_expenses_csrf"] = token
    return token


def _require_csrf() -> None:
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("recurring_expenses_csrf") or ""
    if not supplied or not expected or not secrets.compare_digest(supplied, expected):
        abort(400, "CSRFトークンが一致しません。")


def _month(value: str | None) -> str:
    raw = str(value or "").strip()
    try:
        parsed = datetime.strptime(raw, "%Y-%m")
    except ValueError:
        parsed = datetime.now()
    return parsed.strftime("%Y-%m")


def _optional_int(value, *, minimum: int | None = None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    result = int(raw)
    if minimum is not None and result < minimum:
        raise ValueError
    return result


def _optional_link_url(value) -> str | None:
    url = str(value or "").strip()
    if not url:
        return None
    if len(url) > 2048:
        raise ValueError("リンクURLは2048文字以内で入力してください。")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("リンクURLはhttp://またはhttps://から入力してください。")
    return url


def _master_data() -> dict:
    try:
        return freee_services.fetch_freee_master_bundle()
    except Exception as exc:
        return {
            "companies": [], "account_items": [], "items": [], "taxes": [],
            "walletables": [], "partners": [],
            "warnings": [freee_services.sanitize_freee_error(str(exc))],
        }


def _master_draft(form, master_id: int | None) -> dict:
    draft = form.to_dict(flat=True)
    draft["id"] = master_id
    draft["receipt_required"] = 1 if form.get("receipt_required") == "1" else 0
    draft["is_active"] = 1 if form.get("is_active") == "1" else 0
    for key in ("frequency_months", "account_item_id", "item_id", "partner_id", "tax_code", "walletable_id"):
        raw = str(draft.get(key) or "").strip()
        if raw and raw.lstrip("-").isdigit():
            draft[key] = int(raw)
    return draft


def _batch_candidate_ids(items: list[dict]) -> list[int]:
    return [
        int(item["id"])
        for item in items
        if item.get("status") not in COMPLETED_MONTH_STATUSES
    ]


@recurring_expenses_bp.get("/")
def index():
    selected_month = _month(request.args.get("month"))
    items = list_month_items(selected_month)
    for item in items:
        item["attachments"] = list_attachments(int(item["id"]))
    edit_id = _optional_int(request.args.get("edit"), minimum=1)
    draft = session.pop(MASTER_DRAFT_SESSION_KEY, None)
    if draft and draft.get("return_month") == selected_month:
        editing = draft
    else:
        editing = get_master(edit_id) if edit_id else None
    manage_masters = request.args.get("manage") == "1" or editing is not None
    master_data = _master_data() if manage_masters else {
        "companies": [], "account_items": [], "items": [], "taxes": [],
        "walletables": [], "partners": [], "warnings": [],
    }
    summary = {
        "count": len(items),
        "registered": sum(item["status"] in {"registered", "manual"} for item in items),
        "pending": len(_batch_candidate_ids(items)),
        "overdue": sum(
            item["status"] not in {"registered", "manual", "excluded"} and item["issue_date"] < date.today()
            for item in items
        ),
        "amount": sum(int(item.get("actual_amount") or 0) for item in items if item["status"] != "excluded"),
    }
    return render_template(
        "recurring_expenses/index.html",
        selected_month=selected_month,
        items=items,
        masters=list_masters(),
        editing=editing,
        master_data=master_data,
        manage_masters=manage_masters,
        summary=summary,
        today=date.today(),
        csrf_token=_csrf_token(),
    )


@recurring_expenses_bp.post("/masters/save")
def master_save():
    _require_csrf()
    master_id = _optional_int(request.form.get("master_id"), minimum=1)
    try:
        name = request.form.get("name", "").strip()
        if not name:
            raise ValueError("経費名を入力してください。")
        amount_mode = request.form.get("amount_mode")
        if amount_mode not in {"fixed", "variable"}:
            raise ValueError("金額種別が不正です。")
        default_amount = _optional_int(request.form.get("default_amount"), minimum=0)
        if amount_mode == "fixed" and default_amount is None:
            raise ValueError("固定金額を入力してください。")
        due_day = _optional_int(request.form.get("due_day"), minimum=1)
        frequency = _optional_int(request.form.get("frequency_months"), minimum=1)
        if not due_day or due_day > 31 or frequency not in {1, 2, 3, 6, 12}:
            raise ValueError("発生日または発生周期が不正です。")
        start_month = _month(request.form.get("start_month"))
        raw_end_month = request.form.get("end_month", "").strip()
        end_month = _month(raw_end_month) if raw_end_month else None
        if end_month and end_month < start_month:
            raise ValueError("終了月は開始月以降にしてください。")
        payment_mode = request.form.get("payment_mode")
        registration_mode = request.form.get("registration_mode")
        if payment_mode not in {"settled", "unsettled"} or registration_mode not in {"create", "existing", "manual"}:
            raise ValueError("freee登録方法が不正です。")
        values = {
            "name": name,
            "link_url": _optional_link_url(request.form.get("link_url")),
            "allow_skip": 1 if request.form.get("allow_skip") == "1" else 0,
            "amount_mode": amount_mode,
            "default_amount": default_amount,
            "due_day": due_day,
            "frequency_months": frequency,
            "start_month": start_month,
            "end_month": end_month,
            "account_item_id": _optional_int(request.form.get("account_item_id"), minimum=1),
            "item_id": _optional_int(request.form.get("item_id"), minimum=1),
            "partner_id": _optional_int(request.form.get("partner_id"), minimum=1),
            "tax_code": _optional_int(request.form.get("tax_code"), minimum=0),
            "payment_mode": payment_mode,
            "walletable_type": request.form.get("walletable_type", "").strip() or None,
            "walletable_id": _optional_int(request.form.get("walletable_id"), minimum=1),
            "registration_mode": registration_mode,
            "receipt_required": 1 if request.form.get("receipt_required") == "1" else 0,
            "notes": request.form.get("notes", "").strip() or None,
            "order_no": _optional_int(request.form.get("order_no")) or 0,
            "is_active": 1 if request.form.get("is_active") == "1" else 0,
        }
        if not values["account_item_id"] or values["tax_code"] is None:
            raise ValueError("勘定科目と税区分を選択してください。")
        if payment_mode == "settled" and (not values["walletable_type"] or not values["walletable_id"]):
            raise ValueError("支払済みの場合は決済口座を選択してください。")
        save_master(values, master_id)
        session.pop(MASTER_DRAFT_SESSION_KEY, None)
        flash(f"定期経費「{name}」を保存しました。", "success")
        return redirect(url_for("recurring_expenses.index", month=_month(request.form.get("return_month"))))
    except (TypeError, ValueError) as exc:
        session[MASTER_DRAFT_SESSION_KEY] = _master_draft(request.form, master_id)
        flash(str(exc) if str(exc) else "入力内容を確認してください。", "danger")
    return redirect(url_for(
        "recurring_expenses.index",
        month=_month(request.form.get("return_month")),
        manage=1,
        edit=master_id,
    ))


@recurring_expenses_bp.post("/partners/create")
def partner_create():
    _require_csrf()
    source = request.get_json(silent=True) or request.form
    try:
        result = create_or_find_partner(
            source.get("name", ""),
            source.get("code"),
            force=str(source.get("force", "")).lower() in {"1", "true", "yes"},
        )
        return jsonify({"ok": True, **result})
    except (ValueError, RuntimeError) as exc:
        return jsonify({
            "ok": False,
            "message": freee_services.sanitize_freee_error(str(exc)),
        }), 400


@recurring_expenses_bp.post("/masters/<int:master_id>/active")
def master_active(master_id: int):
    _require_csrf()
    if not get_master(master_id):
        abort(404)
    set_master_active(master_id, request.form.get("active") == "1")
    flash("経費マスターの状態を変更しました。", "success")
    return redirect(url_for("recurring_expenses.index", month=_month(request.form.get("month"))))


@recurring_expenses_bp.post("/months/<int:month_id>/update")
def month_update(month_id: int):
    _require_csrf()
    item = get_month_item(month_id)
    if not item:
        abort(404)
    is_json = request.is_json
    source = request.get_json(silent=True) or request.form
    try:
        issue_date = datetime.strptime(source.get("issue_date", ""), "%Y-%m-%d").date()
        amount = _optional_int(source.get("actual_amount"), minimum=0)
        freee_memo = str(source.get("freee_memo") or "").strip()
        if len(freee_memo) > 255:
            raise ValueError("freeeメモは255文字以内で入力してください。")
        status = source.get("status") or "pending"
        if status not in STATUSES:
            raise ValueError
        if status == "excluded" and not item.get("allow_skip"):
            raise ValueError("この経費はマスターで「発生なしを許可」がOFFです。")
        existing_deal_id = _optional_int(source.get("existing_deal_id"), minimum=1)
        update_month_item(
            month_id,
            issue_date=issue_date,
            actual_amount=amount,
            freee_memo=freee_memo or None,
            status=status,
            existing_deal_id=existing_deal_id,
        )
        if is_json:
            updated = get_month_item(month_id) or {}
            return jsonify({"ok": True, "status": updated.get("status")})
        flash(f"「{item['name']}」を更新しました。", "success")
    except (TypeError, ValueError) as exc:
        message = str(exc) or "日付・金額・状態を確認してください。"
        if is_json:
            return jsonify({"ok": False, "message": message}), 400
        flash(message, "danger")
    return redirect(url_for("recurring_expenses.index", month=item["target_month"]))


def _validate_upload(upload) -> tuple[bytes, str, str]:
    original_name = Path(upload.filename or "").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("PDF・JPEG・PNG・HEICだけを添付できます。")
    content = upload.read(MAX_ATTACHMENT_BYTES + 1)
    if not content or len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError("ファイルは1件25MB以下にしてください。")
    if suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ValueError("PDFファイルの内容を確認できません。")
        mime_type = "application/pdf"
    else:
        try:
            if suffix in {".heic", ".heif"}:
                from pillow_heif import register_heif_opener

                register_heif_opener()
            from io import BytesIO

            with Image.open(BytesIO(content)) as image:
                image.verify()
        except Exception as exc:
            raise ValueError("画像ファイルの内容を確認できません。") from exc
        mime_type = "image/heic" if suffix in {".heic", ".heif"} else f"image/{'jpeg' if suffix in {'.jpg', '.jpeg'} else 'png'}"
    return content, original_name, mime_type


@recurring_expenses_bp.post("/months/<int:month_id>/attachments")
def attachment_upload(month_id: int):
    _require_csrf()
    item = get_month_item(month_id)
    if not item:
        abort(404)
    uploads = [upload for upload in request.files.getlist("receipts") if upload.filename]
    if not uploads:
        flash("添付するファイルを選択してください。", "warning")
        return redirect(url_for("recurring_expenses.index", month=item["target_month"]))
    saved = 0
    for upload in uploads:
        target = None
        try:
            content, original_name, mime_type = _validate_upload(upload)
            digest = hashlib.sha256(content).hexdigest()
            month_folder = STORAGE_ROOT / item["target_month"]
            folder = month_folder / str(month_id)
            folder.mkdir(parents=True, exist_ok=True)
            for private_folder in (STORAGE_ROOT, month_folder, folder):
                private_folder.chmod(0o700)
            stored_name = f"{uuid.uuid4().hex}{Path(original_name).suffix.lower()}"
            target = folder / secure_filename(stored_name)
            target.write_bytes(content)
            target.chmod(0o600)
            add_attachment(month_id, {
                "original_name": original_name[:255],
                "stored_name": stored_name,
                "file_path": str(target),
                "mime_type": mime_type,
                "file_size": len(content),
                "sha256": digest,
            })
            saved += 1
        except ValueError as exc:
            if target:
                target.unlink(missing_ok=True)
            flash(f"{Path(upload.filename or 'ファイル').name}: {exc}", "danger")
        except Exception as exc:
            if target:
                target.unlink(missing_ok=True)
            message = "同じ内容のファイルはすでに添付されています。" if "Duplicate entry" in str(exc) else "添付ファイルを保存できませんでした。"
            flash(f"{Path(upload.filename or 'ファイル').name}: {message}", "danger")
    if saved:
        flash(f"証憑を{saved}件添付しました。", "success")
    return redirect(url_for("recurring_expenses.index", month=item["target_month"]))


@recurring_expenses_bp.get("/attachments/<int:attachment_id>")
def attachment_view(attachment_id: int):
    attachment = get_attachment(attachment_id)
    if not attachment or not Path(attachment["file_path"]).is_file():
        abort(404)
    return send_file(
        attachment["file_path"],
        mimetype=attachment["mime_type"],
        download_name=attachment["original_name"],
        as_attachment=request.args.get("download") == "1",
        conditional=True,
    )


@recurring_expenses_bp.post("/attachments/<int:attachment_id>/delete")
def attachment_delete(attachment_id: int):
    _require_csrf()
    attachment = get_attachment(attachment_id)
    if not attachment:
        abort(404)
    item = get_month_item(int(attachment["month_id"]))
    deleted = delete_attachment(attachment_id)
    if deleted:
        Path(deleted["file_path"]).unlink(missing_ok=True)
        flash("添付ファイルを削除しました。", "success")
    else:
        flash("freee取引にひも付いている証憑です。先にfreee取引を削除してください。", "warning")
    return redirect(url_for("recurring_expenses.index", month=item["target_month"]))


@recurring_expenses_bp.post("/months/<int:month_id>/register")
def month_register(month_id: int):
    _require_csrf()
    item = get_month_item(month_id)
    if not item:
        abort(404)
    try:
        result = register_month(month_id)
        if result["status"] == "manual":
            flash(f"「{item['name']}」を手動登録済みにしました。", "success")
        else:
            flash(f"「{item['name']}」をfreeeへ登録しました（取引ID: {result['deal_id']}）。", "success")
    except Exception as exc:
        flash(f"freee登録に失敗しました: {freee_services.sanitize_freee_error(str(exc))}", "danger")
    return redirect(url_for("recurring_expenses.index", month=item["target_month"]))


@recurring_expenses_bp.post("/months/<int:month_id>/freee-delete")
def month_freee_delete(month_id: int):
    _require_csrf()
    item = get_month_item(month_id)
    if not item:
        abort(404)
    try:
        result = delete_registered_month(month_id)
        deleted_attachments = delete_month_attachments(month_id)
        for attachment in deleted_attachments:
            Path(attachment["file_path"]).unlink(missing_ok=True)
        if result["status"] == "already_deleted":
            flash(f"「{item['name']}」のfreee取引は既に削除済みでした。登録状態を解除し、MFU側の添付を{len(deleted_attachments)}件削除しました。", "success")
        else:
            flash(f"「{item['name']}」のfreee取引（ID: {result['deal_id']}）とMFU側の添付{len(deleted_attachments)}件を削除しました。", "success")
    except Exception as exc:
        flash(f"freee取引を削除できませんでした: {freee_services.sanitize_freee_error(str(exc))}", "danger")
    return redirect(url_for("recurring_expenses.index", month=item["target_month"]))


@recurring_expenses_bp.post("/months/register-batch")
def month_register_batch():
    _require_csrf()
    target_month = _month(request.form.get("month"))
    month_ids = _batch_candidate_ids(list_month_items(target_month))
    if not month_ids:
        flash("一括登録できる未登録項目はありません。", "info")
        return redirect(url_for("recurring_expenses.index", month=target_month))
    if len(month_ids) > 100:
        abort(400, "一括登録は100件までです。")
    succeeded = 0
    failed = []
    for month_id in month_ids:
        item = get_month_item(month_id)
        if not item or item["target_month"] != target_month:
            continue
        try:
            register_month(month_id)
            succeeded += 1
        except Exception as exc:
            failed.append(f"{item['name']}: {freee_services.sanitize_freee_error(str(exc))}")
    if succeeded:
        flash(f"{succeeded}件を登録しました。", "success")
    if failed:
        flash("一括登録できなかった項目: " + " / ".join(failed), "danger")
    return redirect(url_for("recurring_expenses.index", month=target_month))
