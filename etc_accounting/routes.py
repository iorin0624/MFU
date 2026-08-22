from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for

from app.freee_api import services as freee_services

from . import etc_accounting_bp
from .browser_session import ETCMaintenanceError, ETCTargetPage, etc_maintenance_status, open_etc_login_tab
from .batch import MAX_BATCH_SIZE, evaluate_records, registration_eligibility
from .credentials import (
    credentials_status as load_credentials_status,
    delete_credentials,
    etc_browser_lock,
    save_credentials,
)
from .csv_export import render_csv
from .freee_sync import INTEGRATION_KEY, register_record, update_registered_record
from .manual_jobs import create_manual_fetch_job, read_manual_fetch_job, update_manual_fetch_job
from .notifications import send_test_notification
from .parser import is_provisional_record
from .presentation import (
    format_travel_duration as _format_travel_duration,
    travel_duration_minutes,
)
from .repository import (
    create_batch_job,
    ensure_schema,
    get_batch_items,
    get_batch_job,
    get_record,
    get_records_by_ids,
    get_scheduled_fetch_state,
    list_batch_jobs,
    list_records,
    list_registration_mappings,
    list_runs,
    list_tollgate_operators,
    save_registration_mapping,
)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            flash("ログインが必要です。", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get("user") != "admin":
            flash("管理者のみ操作できます。", "warning")
            return redirect(url_for("etc_accounting.index"))
        return view(*args, **kwargs)

    return wrapper


def _csrf_token() -> str:
    value = session.get("etc_accounting_csrf")
    if not value:
        value = secrets.token_urlsafe(32)
        session["etc_accounting_csrf"] = value
    return value


def _require_csrf() -> None:
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("etc_accounting_csrf") or ""
    if not supplied or not expected or not secrets.compare_digest(supplied, expected):
        abort(400, "CSRFトークンが一致しません。")


def _master_data() -> dict:
    try:
        return freee_services.fetch_freee_master_bundle()
    except Exception as exc:
        return {
            "companies": [], "account_items": [], "items": [], "taxes": [], "walletables": [], "partners": [],
            "warnings": [freee_services.sanitize_freee_error(str(exc))],
        }


def _credentials_status() -> dict:
    try:
        return load_credentials_status()
    except RuntimeError as exc:
        return {"configured": False, "login_id": "", "error": str(exc)}


def _selected_record_ids() -> list[int]:
    values: list[int] = []
    for raw_value in request.form.getlist("record_ids"):
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in values:
            values.append(value)
    if len(values) > MAX_BATCH_SIZE:
        abort(400, f"一括登録は{MAX_BATCH_SIZE}件までです。")
    return values


def _batch_summary(records: list[dict]) -> list[dict]:
    groups: dict[str, dict] = {}
    for record in records:
        registration_number = str(record.get("invoice_registration_number") or "")
        mapping = record.get("registration_mapping") or {}
        group = groups.setdefault(
            registration_number,
            {
                "registration_number": registration_number,
                "issuer_name": record.get("invoice_issuer_name") or "",
                "partner_name": mapping.get("partner_name") or "",
                "item_name": mapping.get("item_name") or "",
                "count": 0,
                "amount": 0,
            },
        )
        group["count"] += 1
        group["amount"] += int(record.get("amount") or 0)
    return list(groups.values())


def _month_options(now: datetime | None = None) -> list[dict]:
    current = now or datetime.now()
    current_index = current.year * 12 + current.month - 1
    options = []
    for offset in range(14 * 12 + 1):
        month_index = current_index - offset
        year, zero_based_month = divmod(month_index, 12)
        month = zero_based_month + 1
        options.append({
            "value": f"{year:04d}{month:02d}",
            "label": f"{year:04d}年{month:02d}月",
        })
    return options


def _sort_batch_records(records: list[dict]) -> list[dict]:
    return sorted(
        records,
        key=lambda record: (
            record.get("used_at") or datetime.max,
            int(record.get("id") or 0),
        ),
    )


def _parse_filter_date(value: str):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _normalize_status_filter(raw_status: str | None) -> str:
    status = "pending" if raw_status is None else raw_status.strip()
    if status not in {"", "pending", "registering", "updating", "registered", "error", "deleted"}:
        return "pending"
    return status


@etc_accounting_bp.before_request
def _ensure_schema() -> None:
    ensure_schema()


@etc_accounting_bp.get("/")
@login_required
def index():
    status = _normalize_status_filter(request.args.get("status"))
    date_from_text = (request.args.get("date_from") or "").strip()
    date_to_text = (request.args.get("date_to") or "").strip()
    selected_operator = (request.args.get("operator") or "").strip()
    operator_options = list_tollgate_operators()
    if selected_operator not in {*operator_options, "__unmatched__"}:
        selected_operator = ""
    settings = freee_services.get_freee_deal_settings(INTEGRATION_KEY) or {}
    registration_mappings = list_registration_mappings(settings.get("company_id"))
    mappings = {
        row["registration_number"]: row
        for row in registration_mappings
    }
    records = list_records(
        status=status,
        limit=None,
        date_from=_parse_filter_date(date_from_text),
        date_to=_parse_filter_date(date_to_text),
        operator_name=selected_operator,
    )
    for record in records:
        record["source_deleted"] = record.get("source_state") == "deleted"
        record["is_provisional"] = is_provisional_record(record)
        record["travel_duration"] = _format_travel_duration(
            record.get("entry_at"),
            record.get("exit_at"),
        )
        record["travel_duration_minutes"] = travel_duration_minutes(
            record.get("entry_at"),
            record.get("exit_at"),
        )
        record_registration_number = str(record.get("invoice_registration_number") or "")
        record["registration_mapping"] = mappings.get(record_registration_number)
        record["registration_mapping_ready"] = bool(
            record_registration_number and (record["registration_mapping"] or {}).get("configured")
        )
        record["batch_eligible"], record["batch_reason"] = registration_eligibility(
            record,
            company_id=int(settings.get("company_id") or 0) or None,
            mapping=record["registration_mapping"],
        )
    return render_template(
        "etc_accounting/index.html",
        records=records,
        runs=list_runs(),
        selected_status=status,
        freee_connected=bool(freee_services.load_freee_token_row()),
        settings=settings,
        is_admin=session.get("user") == "admin",
        batch_jobs=list_batch_jobs(5) if session.get("user") == "admin" else [],
        month_options=_month_options(),
        scheduled_fetch_state=get_scheduled_fetch_state(),
        selected_date_from=date_from_text,
        selected_date_to=date_to_text,
        selected_operator=selected_operator,
        operator_options=operator_options,
        csrf_token=_csrf_token(),
    )


@etc_accounting_bp.get("/export.csv")
@login_required
def export_csv():
    scope = (request.args.get("scope") or "filtered").strip()
    if scope == "all":
        status = ""
        date_from = None
        date_to = None
        selected_operator = ""
    else:
        status = _normalize_status_filter(request.args.get("status"))
        date_from = _parse_filter_date(request.args.get("date_from") or "")
        date_to = _parse_filter_date(request.args.get("date_to") or "")
        selected_operator = (request.args.get("operator") or "").strip()
        if selected_operator not in {*list_tollgate_operators(), "__unmatched__"}:
            selected_operator = ""

    records = list_records(
        status=status,
        limit=None,
        date_from=date_from,
        date_to=date_to,
        operator_name=selected_operator,
    )
    filename = f"etc_meisai_{datetime.now():%Y%m%d_%H%M}.csv"
    return Response(
        render_csv(records),
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@etc_accounting_bp.post("/browser/start")
@login_required
def browser_start():
    _require_csrf()
    try:
        with etc_browser_lock():
            result = open_etc_login_tab()
        maintenance = etc_maintenance_status()
        return jsonify({
            "ok": True,
            "url": result.get("url"),
            "maintenance": bool(maintenance.get("active")),
            "warning": maintenance.get("message") or None,
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@etc_accounting_bp.get("/browser/status")
@login_required
def browser_status():
    try:
        with etc_browser_lock(), ETCTargetPage() as browser:
            browser.ensure_logged_in()
            return jsonify({"ok": True, "loggedIn": True, "maintenance": False, "autoLoginConfigured": True})
    except ETCMaintenanceError as exc:
        return jsonify({
            "ok": True,
            "loggedIn": False,
            "maintenance": True,
            "autoLoginConfigured": _credentials_status().get("configured", False),
            "warning": str(exc),
            "error": str(exc),
        })
    except RuntimeError as exc:
        return jsonify({
            "ok": True,
            "loggedIn": False,
            "maintenance": False,
            "autoLoginConfigured": _credentials_status().get("configured", False),
            "warning": str(exc),
            "error": str(exc),
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


@etc_accounting_bp.post("/discord/test")
@login_required
@admin_required
def discord_test():
    _require_csrf()
    try:
        sent_count = send_test_notification()
        flash(f"Discordへ最新のETC利用明細{sent_count}件をテスト通知しました。", "success")
    except Exception as exc:
        flash(f"Discordテスト通知に失敗しました: {exc}", "danger")
    return redirect(url_for("etc_accounting.index"))


@etc_accounting_bp.post("/refresh")
@login_required
def refresh():
    _require_csrf()
    month = (request.form.get("month") or "").strip()
    if not re.fullmatch(r"20\d{4}", month):
        if request.accept_mimetypes.best == "application/json":
            return jsonify({"ok": False, "error": "対象月はYYYYMM形式で指定してください。"}), 400
        flash("対象月はYYYYMM形式で指定してください。", "warning")
        return redirect(url_for("etc_accounting.index"))
    root = str(Path(current_app.root_path).parent)
    job_id = create_manual_fetch_job(month)
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.etc_accounting.fetch_cli",
                "--month",
                month,
                "--manual-job-id",
                job_id,
            ],
            cwd=root,
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        update_manual_fetch_job(job_id, status="error", error=str(exc))
        if request.accept_mimetypes.best == "application/json":
            return jsonify({"ok": False, "error": str(exc)}), 500
        flash(f"ETC明細の取得を開始できませんでした: {exc}", "danger")
        return redirect(url_for("etc_accounting.index"))
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"ok": True, "status": "pending", "jobId": job_id, "month": month}), 202
    flash(f"{month[:4]}年{month[4:]}月分の取得を開始しました。結果は取得履歴で確認できます。", "info")
    return redirect(url_for("etc_accounting.index"))


@etc_accounting_bp.get("/refresh/jobs/<job_id>")
@login_required
def refresh_job_status(job_id: str):
    try:
        job = read_manual_fetch_job(job_id)
    except ValueError:
        job = None
    if not job:
        return jsonify({"ok": False, "error": "手動取得ジョブが見つかりません。"}), 404
    return jsonify({"ok": True, **job})


@etc_accounting_bp.get("/<int:record_id>/pdf")
@login_required
def pdf(record_id: int):
    record = get_record(record_id)
    if not record or not record.get("pdf_path"):
        abort(404)
    path = Path(record["pdf_path"])
    if not path.is_file():
        abort(404)
    return send_file(path, mimetype="application/pdf", as_attachment=False, download_name=path.name)


@etc_accounting_bp.post("/<int:record_id>/register")
@login_required
def register(record_id: int):
    _require_csrf()
    if request.form.get("confirm") != "1":
        flash("登録確認がありません。", "warning")
        return redirect(url_for("etc_accounting.index"))
    try:
        result = register_record(record_id)
        if result.get("status") == "already_registered":
            flash("この明細は既にfreeeへ登録済みです。", "info")
        else:
            flash(f"freeeへ登録しました。取引ID: {result['deal_id']}", "success")
    except Exception as exc:
        flash(f"freee API登録に失敗しました: {freee_services.sanitize_freee_error(str(exc))}", "danger")
    return redirect(url_for("etc_accounting.index", status=request.form.get("return_status") or "pending"))


@etc_accounting_bp.post("/batch/confirm")
@login_required
@admin_required
def batch_confirm():
    _require_csrf()
    record_ids = _selected_record_ids()
    if not record_ids:
        flash("一括登録する明細を選択してください。", "warning")
        return redirect(url_for("etc_accounting.index"))
    records = get_records_by_ids(record_ids)
    settings = freee_services.get_freee_deal_settings(INTEGRATION_KEY) or {}
    eligible, excluded = evaluate_records(
        records,
        company_id=int(settings.get("company_id") or 0) or None,
    )
    eligible = _sort_batch_records(eligible)
    return render_template(
        "etc_accounting/batch_confirm.html",
        eligible=eligible,
        excluded=excluded,
        groups=_batch_summary(eligible),
        total_amount=sum(int(record.get("amount") or 0) for record in eligible),
        csrf_token=_csrf_token(),
    )


@etc_accounting_bp.post("/batch/start")
@login_required
@admin_required
def batch_start():
    _require_csrf()
    if request.form.get("confirm") != "1":
        abort(400, "登録確認がありません。")
    record_ids = _selected_record_ids()
    records = get_records_by_ids(record_ids)
    settings = freee_services.get_freee_deal_settings(INTEGRATION_KEY) or {}
    eligible, excluded = evaluate_records(
        records,
        company_id=int(settings.get("company_id") or 0) or None,
    )
    eligible = _sort_batch_records(eligible)
    if not eligible:
        flash("現在、一括登録できる明細はありません。", "warning")
        return redirect(url_for("etc_accounting.index"))
    job_id = create_batch_job(
        requested_by=str(session.get("user") or ""),
        record_ids=[int(record["id"]) for record in eligible],
        total_amount=sum(int(record.get("amount") or 0) for record in eligible),
    )
    root = str(Path(current_app.root_path).parent)
    subprocess.Popen(
        [sys.executable, "-m", "app.etc_accounting.batch_cli", "--job-id", job_id],
        cwd=root,
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    if excluded:
        flash(f"状態が変わった{len(excluded)}件を除外し、{len(eligible)}件の一括登録を開始しました。", "warning")
    else:
        flash(f"{len(eligible)}件の一括登録を開始しました。", "info")
    return redirect(url_for("etc_accounting.batch_progress", job_id=job_id))


@etc_accounting_bp.get("/batch/<job_id>")
@login_required
@admin_required
def batch_progress(job_id: str):
    job = get_batch_job(job_id)
    if not job:
        abort(404)
    return render_template(
        "etc_accounting/batch_progress.html",
        job=job,
        items=get_batch_items(job_id),
        csrf_token=_csrf_token(),
    )


@etc_accounting_bp.get("/batch/<job_id>/status")
@login_required
@admin_required
def batch_status(job_id: str):
    job = get_batch_job(job_id)
    if not job:
        abort(404)
    items = get_batch_items(job_id)
    return jsonify({
        "job": {
            "id": job["id"],
            "status": job["status"],
            "total_count": int(job.get("total_count") or 0),
            "success_count": int(job.get("success_count") or 0),
            "failure_count": int(job.get("failure_count") or 0),
            "skipped_count": int(job.get("skipped_count") or 0),
            "total_amount": int(job.get("total_amount") or 0),
            "error_text": job.get("error_text") or "",
        },
        "items": [
            {
                "id": int(item["id"]),
                "record_id": int(item["record_id"]),
                "status": item["status"],
                "deal_id": item.get("freee_deal_id"),
                "error": item.get("error_text") or "",
            }
            for item in items
        ],
    })


@etc_accounting_bp.post("/<int:record_id>/update-freee")
@login_required
def update_freee(record_id: int):
    _require_csrf()
    if request.form.get("confirm") != "1":
        flash("更新確認がありません。", "warning")
        return redirect(url_for("etc_accounting.index", status="registered"))
    try:
        result = update_registered_record(record_id)
        if result.get("status") == "reregistered":
            flash(
                f"freee側で削除済みだったため、新しい取引として再登録しました。取引ID: {result['deal_id']}",
                "success",
            )
        else:
            flash(f"freee登録内容を更新しました。取引ID: {result['deal_id']}", "success")
    except Exception as exc:
        flash(f"freee登録内容を更新できませんでした: {freee_services.sanitize_freee_error(str(exc))}", "danger")
    return redirect(url_for("etc_accounting.index", status=request.form.get("return_status") or "registered"))


@etc_accounting_bp.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    if request.method == "POST":
        _require_csrf()
        walletable = (request.form.get("walletable") or "").strip()
        walletable_type = None
        walletable_id = None
        if ":" in walletable:
            walletable_type, raw_id = walletable.split(":", 1)
            walletable_id = int(raw_id) if raw_id else None
        try:
            freee_services.upsert_freee_integration_settings(
                integration_key=INTEGRATION_KEY,
                account_item_id=int(request.form.get("account_item_id") or 0) or None,
                tax_code=int(request.form.get("tax_code")) if request.form.get("tax_code") else None,
                deal_payment_mode=request.form.get("deal_payment_mode") or "settled",
                walletable_type=walletable_type,
                walletable_id=walletable_id,
                partner_id=int(request.form.get("partner_id") or 0) or None,
                partner_code=(request.form.get("partner_code") or "").strip() or None,
            )
            flash("ETCのfreee登録設定を保存しました。", "success")
        except Exception as exc:
            flash(f"設定を保存できませんでした: {exc}", "danger")
        return redirect(url_for("etc_accounting.settings"))

    master = _master_data()
    current = freee_services.get_freee_integration_settings(INTEGRATION_KEY) or {}
    deal_settings = freee_services.get_freee_deal_settings(INTEGRATION_KEY) or {}
    company_id = master.get("selected_company_id") or deal_settings.get("company_id")
    return render_template(
        "etc_accounting/settings.html",
        master=master,
        current=current,
        registration_mappings=list_registration_mappings(company_id),
        etc_credentials=_credentials_status(),
        csrf_token=_csrf_token(),
        tax_code=freee_services.freee_tax_code,
        walletable_id=freee_services.freee_walletable_id,
        walletable_type=freee_services.freee_walletable_type,
        format_account=freee_services.format_freee_account_item_label,
        format_tax=freee_services.format_freee_tax_label,
        format_wallet=freee_services.format_freee_walletable_label,
        format_partner=freee_services.format_freee_partner_label,
        format_item=freee_services.format_freee_item_label,
    )


@etc_accounting_bp.post("/registration-mappings/<registration_number>")
@login_required
@admin_required
def registration_mapping_save(registration_number: str):
    _require_csrf()
    registration_number = (registration_number or "").strip().upper()
    if not re.fullmatch(r"T\d{13}", registration_number):
        flash("登録番号の形式が正しくありません。", "danger")
        return redirect(url_for("etc_accounting.settings") + "#registration-mappings")
    try:
        settings = freee_services.get_freee_deal_settings(INTEGRATION_KEY) or {}
        company_id = int(settings.get("company_id") or 0)
        if not company_id:
            raise RuntimeError("freee事業所が設定されていません。")
        partner_id = int(request.form.get("partner_id") or 0)
        item_id = int(request.form.get("item_id") or 0)
        master = freee_services.fetch_freee_master_bundle(company_id)
        partners = {int(row["id"]): row for row in master.get("partners") or [] if row.get("id")}
        items = {int(row["id"]): row for row in master.get("items") or [] if row.get("id")}
        partner = partners.get(partner_id)
        item = items.get(item_id)
        if not partner or not item:
            raise RuntimeError("freeeに存在する取引先と品目を選択してください。")
        save_registration_mapping(
            company_id=company_id,
            registration_number=registration_number,
            partner_id=partner_id,
            partner_name=str(partner.get("name") or partner.get("display_name") or ""),
            item_id=item_id,
            item_name=str(item.get("name") or item.get("display_name") or ""),
        )
        flash(f"登録番号 {registration_number} の取引先・品目を保存しました。", "success")
    except Exception as exc:
        flash(f"登録番号別設定を保存できませんでした: {freee_services.sanitize_freee_error(str(exc))}", "danger")
    return redirect(url_for("etc_accounting.settings") + "#registration-mappings")


@etc_accounting_bp.post("/credentials")
@login_required
@admin_required
def credentials_save():
    _require_csrf()
    try:
        save_credentials(request.form.get("login_id") or "", request.form.get("password") or "")
        flash("ETC自動ログイン情報を暗号化して保存しました。", "success")
    except Exception as exc:
        flash(f"ETC自動ログイン情報を保存できませんでした: {exc}", "danger")
    return redirect(url_for("etc_accounting.settings"))


@etc_accounting_bp.post("/credentials/delete")
@login_required
@admin_required
def credentials_delete():
    _require_csrf()
    delete_credentials()
    flash("ETC自動ログイン情報を削除しました。", "success")
    return redirect(url_for("etc_accounting.settings"))
