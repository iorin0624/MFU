from __future__ import annotations

import os
import threading
from functools import wraps

from flask import abort, flash, redirect, render_template, request, send_file, session, url_for

from app.freee_api import services as freee_services

from . import receipt_ocr_bp
from .services import (
    analyze_receipt_with_openai,
    apply_rule_to_record,
    ensure_receipt_ocr_schema,
    find_rule,
    get_tags_for_record,
    infer_receipt_date,
    int_or_none,
    now_jst,
    parse_date_or_none,
    receipt_needs_freee_resync,
    save_upload,
    set_record_tags,
    split_tags,
    sync_record_to_freee,
    upsert_rule_from_record,
)
from app.utils.db import get_db

_schema_lock = threading.Lock()
_schema_ready = False


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            flash("ログインが必要です。", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapper


@receipt_ocr_bp.before_app_request
def _ensure_schema_once() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        ensure_receipt_ocr_schema()
        _schema_ready = True


def _fetchone_dict(cur):
    row = cur.fetchone()
    if row is None or isinstance(row, dict):
        return row
    return dict(zip([d[0] for d in cur.description], row))


def _fetchall_dict(cur):
    rows = cur.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return rows
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in rows]


def _get_record(record_id: int) -> dict | None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM receipt_ocr_records WHERE id=%s", (record_id,))
        record = cur.fetchone()
        if record:
            record["receipt_date"] = infer_receipt_date(record)
            record["management_tags"] = get_tags_for_record(cur, record_id)
            record["freee_needs_resync"] = receipt_needs_freee_resync(record)
        return record
    finally:
        cur.close()
        db.close()


def _master_data():
    try:
        master = freee_services.fetch_freee_master_bundle()
    except Exception as exc:
        return {"companies": [], "account_items": [], "taxes": [], "walletables": [], "partners": [], "warnings": [str(exc)]}
    return master


def _tax_code(tax):
    return freee_services.freee_tax_code(tax)


def _wallet_value(walletable):
    walletable_type = freee_services.freee_walletable_type(walletable)
    walletable_id = freee_services.freee_walletable_id(walletable)
    return f"{walletable_type}:{walletable_id}" if walletable_type and walletable_id else ""


def _record_from_form(form, existing: dict | None = None) -> dict:
    walletable_type = (form.get("walletable_type") or "").strip() or None
    walletable_id = int_or_none(form.get("walletable_id"))
    walletable_value = (form.get("walletable") or "").strip()
    if walletable_value and ":" in walletable_value:
        walletable_type, raw_id = walletable_value.split(":", 1)
        walletable_id = int_or_none(raw_id)
    return {
        "store_name": (form.get("store_name") or "").strip() or None,
        "invoice_registration_number": (form.get("invoice_registration_number") or "").strip() or None,
        "receipt_date": parse_date_or_none(form.get("receipt_date")),
        "total_amount_yen": int_or_none(form.get("total_amount_yen")),
        "tax10_amount_yen": int_or_none(form.get("tax10_amount_yen")),
        "tax8_amount_yen": int_or_none(form.get("tax8_amount_yen")),
        "account_item_id": int_or_none(form.get("account_item_id")),
        "tax_code_10": int_or_none(form.get("tax_code_10")),
        "tax_code_8": int_or_none(form.get("tax_code_8")),
        "tax_code_nontax": int_or_none(form.get("tax_code_nontax")),
        "walletable_type": walletable_type,
        "walletable_id": walletable_id,
        "freee_partner_id": int_or_none(form.get("freee_partner_id")),
        "freee_partner_code": (form.get("freee_partner_code") or "").strip() or None,
        "freee_memo_tags": (form.get("freee_memo_tags") or "").strip() or None,
        "memo": (form.get("memo") or "").strip() or None,
    }


@receipt_ocr_bp.get("/")
@login_required
def index():
    q = request.args.get("q", "").strip()
    params = []
    where = ["1=1"]
    if q:
        where.append("(store_name LIKE %s OR invoice_registration_number LIKE %s OR memo LIKE %s)")
        like = f"%{q}%"
        params.extend([like, like, like])
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            "SELECT * FROM receipt_ocr_records WHERE " + " AND ".join(where) + " ORDER BY id DESC LIMIT 300",
            params,
        )
        records = cur.fetchall()
    finally:
        cur.close()
        db.close()
    for record in records:
        record["receipt_date"] = infer_receipt_date(record)
        record["freee_needs_resync"] = receipt_needs_freee_resync(record)
    return render_template("receipt_ocr_list.html", records=records, q=q)


@receipt_ocr_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "GET":
        return render_template("receipt_ocr_new.html")
    upload = request.files.get("receipt_image")
    if not upload or not upload.filename:
        flash("レシート画像を選択してください。", "warning")
        return redirect(url_for("receipt_ocr.new"))
    try:
        original_path, processed_path = save_upload(upload)
        ocr = analyze_receipt_with_openai(processed_path)
        now = now_jst()
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO receipt_ocr_records (
                owner_user_id, status, store_name, invoice_registration_number,
                receipt_date, total_amount_yen, tax10_amount_yen, tax8_amount_yen,
                original_filename, original_image_path, processed_image_path,
                ocr_text, ocr_json, created_at, updated_at
            ) VALUES (%s, 'draft', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                session.get("user"), ocr.get("store_name"), ocr.get("invoice_registration_number"),
                parse_date_or_none(ocr.get("date")), int_or_none(ocr.get("total_amount_yen")),
                int_or_none(ocr.get("tax10_amount_yen")), int_or_none(ocr.get("tax8_amount_yen")),
                upload.filename, original_path, processed_path, ocr.get("raw_text"),
                __import__("json").dumps(ocr, ensure_ascii=False), now, now,
            ),
        )
        record_id = int(cur.lastrowid)
        rule = find_rule(cur, ocr.get("invoice_registration_number"), ocr.get("store_name"))
        apply_rule_to_record(cur, record_id, rule)
        db.commit()
        db.close()
        flash("OCR解析が完了しました。内容を確認してください。", "success")
        return redirect(url_for("receipt_ocr.edit", record_id=record_id))
    except Exception as exc:
        flash(f"OCR処理に失敗しました: {exc}", "danger")
        return redirect(url_for("receipt_ocr.new"))


@receipt_ocr_bp.get("/<int:record_id>")
@login_required
def detail(record_id: int):
    record = _get_record(record_id)
    if not record:
        abort(404)
    return render_template("receipt_ocr_detail.html", record=record)


@receipt_ocr_bp.route("/<int:record_id>/edit", methods=["GET", "POST"])
@login_required
def edit(record_id: int):
    record = _get_record(record_id)
    if not record:
        abort(404)
    if request.method == "POST":
        payload = _record_from_form(request.form, record)
        tags = split_tags(request.form.get("management_tags"))
        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute(
                """
                UPDATE receipt_ocr_records
                SET store_name=%s, invoice_registration_number=%s, receipt_date=%s,
                    total_amount_yen=%s, tax10_amount_yen=%s, tax8_amount_yen=%s,
                    account_item_id=%s, tax_code_10=%s, tax_code_8=%s, tax_code_nontax=%s,
                    walletable_type=%s, walletable_id=%s, freee_partner_id=%s, freee_partner_code=%s,
                    freee_memo_tags=%s, memo=%s, updated_at=%s
                WHERE id=%s
                """,
                (
                    payload["store_name"], payload["invoice_registration_number"], payload["receipt_date"],
                    payload["total_amount_yen"], payload["tax10_amount_yen"], payload["tax8_amount_yen"],
                    payload["account_item_id"], payload["tax_code_10"], payload["tax_code_8"], payload["tax_code_nontax"],
                    payload["walletable_type"], payload["walletable_id"], payload["freee_partner_id"], payload["freee_partner_code"],
                    payload["freee_memo_tags"], payload["memo"], now_jst(), record_id,
                ),
            )
            set_record_tags(cur, record_id, tags)
            cur.execute("SELECT * FROM receipt_ocr_records WHERE id=%s", (record_id,))
            updated = cur.fetchone()
            upsert_rule_from_record(cur, updated, tags, payload["freee_memo_tags"])
            db.commit()
            flash("保存しました。同じ店舗/T番号の自動設定にも反映しました。", "success")
            return redirect(url_for("receipt_ocr.detail", record_id=record_id))
        finally:
            cur.close()
            db.close()
    master = _master_data()
    return render_template(
        "receipt_ocr_edit.html",
        record=record,
        master=master,
        tax_code=_tax_code,
        wallet_value=_wallet_value,
        management_tags=" ".join(record.get("management_tags") or []),
    )


@receipt_ocr_bp.post("/<int:record_id>/freee-api")
@login_required
def freee_api_sync(record_id: int):
    record = _get_record(record_id)
    if not record:
        abort(404)
    try:
        result = sync_record_to_freee(record)
        if result.get("status") == "updated":
            flash("freeeの支出取引を更新しました。", "success")
        elif result.get("status") == "skipped_already_synced":
            flash("freeeは同期済みです。", "info")
        else:
            flash("freeeへ支出取引を登録しました。", "success")
    except Exception as exc:
        flash(f"freee API登録に失敗しました: {exc}", "danger")
    return redirect(url_for("receipt_ocr.detail", record_id=record_id))


@receipt_ocr_bp.post("/<int:record_id>/reanalyze")
@login_required
def reanalyze(record_id: int):
    record = _get_record(record_id)
    if not record:
        abort(404)
    image_path = record.get("processed_image_path") or record.get("original_image_path")
    if not image_path or not os.path.exists(image_path):
        flash("再OCRできる画像が見つかりません。", "warning")
        return redirect(url_for("receipt_ocr.detail", record_id=record_id))
    try:
        ocr = analyze_receipt_with_openai(image_path)
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """
            UPDATE receipt_ocr_records
            SET store_name=%s,
                invoice_registration_number=%s,
                receipt_date=%s,
                total_amount_yen=%s,
                tax10_amount_yen=%s,
                tax8_amount_yen=%s,
                ocr_text=%s,
                ocr_json=%s,
                updated_at=%s
            WHERE id=%s
            """,
            (
                ocr.get("store_name"),
                ocr.get("invoice_registration_number"),
                parse_date_or_none(ocr.get("date")),
                int_or_none(ocr.get("total_amount_yen")),
                int_or_none(ocr.get("tax10_amount_yen")),
                int_or_none(ocr.get("tax8_amount_yen")),
                ocr.get("raw_text"),
                __import__("json").dumps(ocr, ensure_ascii=False),
                now_jst(),
                record_id,
            ),
        )
        rule = find_rule(cur, ocr.get("invoice_registration_number"), ocr.get("store_name"))
        apply_rule_to_record(cur, record_id, rule)
        db.commit()
        db.close()
        flash("改善版OCRで再解析しました。", "success")
    except Exception as exc:
        flash(f"再OCRに失敗しました: {exc}", "danger")
    return redirect(url_for("receipt_ocr.edit", record_id=record_id))


@receipt_ocr_bp.get("/<int:record_id>/image/<kind>")
@login_required
def image(record_id: int, kind: str):
    record = _get_record(record_id)
    if not record:
        abort(404)
    path = record.get("processed_image_path") if kind == "processed" else record.get("original_image_path")
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path)


@receipt_ocr_bp.get("/rules")
@login_required
def rules():
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT * FROM receipt_ocr_rules ORDER BY updated_at DESC, id DESC")
        rows = cur.fetchall()
    finally:
        cur.close()
        db.close()
    return render_template("receipt_ocr_rules.html", rules=rows)
