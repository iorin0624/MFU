from __future__ import annotations

import threading
import os
import hashlib
import hmac
import secrets
from functools import wraps
from pathlib import Path

import requests
from flask import abort, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for

from app.bank_account.integration_service import issue_payout_access_token_for_invoice
from app.payment.checkout_otp import (
    CheckoutOtpError,
    consume_checkout_otp,
    is_checkout_otp_verified,
    mask_email,
    require_checkout_otp,
    send_checkout_otp,
    verify_checkout_otp,
)
from app.payment.square_gateway import (
    SquareTransportError,
    request_square,
    square_error_info,
)
from app.payment.square_state import (
    is_payment_completed,
    normalize_square_status,
    should_apply_square_update,
    square_datetime,
)

from . import invoice_bp
from .freee_csv import build_invoice_freee_csv_response, build_invoice_freee_csv
from .freee_api import invoice_needs_freee_resync, sync_invoice_to_freee
from .mail import send_invoice_mail, send_invoice_receipt_mail
from .pdf import generate_invoice_pdf, generate_invoice_receipt_pdf
from .services import (
    BANK_INFO_MODE_LABELS,
    BANK_INFO_MODE_PAYOUT_LINK,
    INVOICE_DELETABLE_STATUSES,
    InvoiceValidationError,
    apply_issuer_template_to_form_data,
    build_default_invoice_mail_body,
    build_invoice_mail_body_with_payment_guidance,
    build_invoice_card_payment_url,
    build_issuer_template_form_data,
    build_fuel_cost_helper,
    build_invoice_form_data,
    create_issuer_template,
    delete_contact,
    delete_issuer_template,
    duplicate_invoice,
    ensure_invoice_schema,
    ensure_invoice_card_payment_token,
    fetch_contacts,
    get_default_issuer_template,
    get_contact,
    get_invoice_effective_bank_info,
    get_invoice,
    get_invoice_by_card_payment_token,
    get_invoice_card_payment_by_square_payment_id,
    get_invoice_card_payment_by_id,
    get_issuer_template_by_id,
    get_latest_invoice_sent_mail_log,
    get_latest_invoice_card_payment,
    get_invoice_square_config,
    has_invoice_receipt_mail_sent,
    list_invoices,
    list_deleted_invoices,
    list_issuer_templates,
    log_csv_export,
    mark_invoice_issued,
    merge_invoice_cc_emails,
    notify_invoice_card_payment_if_needed,
    normalize_bank_info_mode,
    parse_invoice_items,
    resolve_invoice_issuer_email,
    save_contact,
    save_invoice,
    soft_delete_invoice,
    restore_deleted_invoice,
    purge_deleted_invoice,
    create_invoice_card_payment_pending,
    save_invoice_payout_token,
    update_invoice_card_payment_result,
    mark_invoice_paid_by_card,
    set_default_issuer_template,
    ensure_invoice_square_customer,
    update_issuer_template,
    update_invoice_status,
    get_db,
)
from .utils import (
    DEFAULT_TAX_CATEGORY,
    ROW_TYPE_NORMAL,
    STATUS_BADGES,
    STATUS_LABELS,
    TAX_CATEGORY_LABELS,
    TAX_MODE_LABELS,
    build_mail_subject,
    default_due_date,
    format_ymd,
    now_jst,
    parse_date,
)

_schema_lock = threading.Lock()
_schema_ready = False
_INVOICE_DELETE_CSRF_SESSION_KEY = "invoice_delete_csrf"


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            flash("ログインが必要です。", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


def _invoice_delete_csrf_token() -> str:
    token = str(session.get(_INVOICE_DELETE_CSRF_SESSION_KEY) or "")
    if not token:
        token = secrets.token_urlsafe(32)
        session[_INVOICE_DELETE_CSRF_SESSION_KEY] = token
    return token


def _require_invoice_delete_csrf() -> None:
    expected = str(session.get(_INVOICE_DELETE_CSRF_SESSION_KEY) or "")
    supplied = str(request.form.get("csrf_token") or "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400)


def _delete_stored_invoice_pdf(pdf_storage_path: str | None) -> bool:
    if not pdf_storage_path:
        return True
    storage_root = Path(current_app.config.get("INVOICE_PDF_DIR") or "/tmp/mfu/invoice_pdf").resolve()
    candidate = Path(pdf_storage_path).resolve()
    try:
        candidate.relative_to(storage_root)
    except ValueError:
        current_app.logger.error("refused invoice PDF deletion outside storage root: %s", candidate)
        return False
    try:
        candidate.unlink(missing_ok=True)
        return True
    except OSError:
        current_app.logger.exception("invoice PDF deletion failed: %s", candidate)
        return False


@invoice_bp.before_app_request
def _ensure_schema_once() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        ensure_invoice_schema()
        _schema_ready = True


@invoice_bp.app_template_filter("invoice_status_label")
def invoice_status_label(value: str) -> str:
    return STATUS_LABELS.get(value, value)


@invoice_bp.app_template_filter("invoice_status_badge")
def invoice_status_badge(value: str) -> str:
    return STATUS_BADGES.get(value, "bg-secondary")


@invoice_bp.app_template_filter("invoice_tax_mode_label")
def invoice_tax_mode_label(value: str) -> str:
    return TAX_MODE_LABELS.get(value, value)


@invoice_bp.app_template_filter("yen")
def fmt_yen(value) -> str:
    return f"{int(value or 0):,}"


@invoice_bp.get("/")
@login_required
def index():
    return redirect(url_for("invoice.invoice_list"))


@invoice_bp.get("/list")
@login_required
def invoice_list():
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip()
    start = (request.args.get("start") or "").strip()
    end = (request.args.get("end") or "").strip()
    invoices = list_invoices(q=q, status=status, start=start, end=end)
    return render_template(
        "invoice_list.html",
        invoices=invoices,
        q=q,
        status=status,
        start=start,
        end=end,
        status_labels=STATUS_LABELS,
        deletable_statuses=INVOICE_DELETABLE_STATUSES,
        delete_csrf_token=_invoice_delete_csrf_token(),
    )


@invoice_bp.get("/deleted")
@login_required
def invoice_deleted_list():
    q = (request.args.get("q") or "").strip()
    return render_template(
        "invoice_deleted_list.html",
        invoices=list_deleted_invoices(q=q),
        q=q,
        status_labels=STATUS_LABELS,
        delete_csrf_token=_invoice_delete_csrf_token(),
    )


@invoice_bp.post("/<int:invoice_id>/delete")
@login_required
def invoice_delete(invoice_id: int):
    _require_invoice_delete_csrf()
    try:
        soft_delete_invoice(invoice_id, deleted_by=str(session.get("user") or ""))
        flash("請求書を削除済み一覧へ移動しました。", "success")
    except InvoiceValidationError as exc:
        flash(str(exc), "warning")
    return redirect(url_for("invoice.invoice_list"))


@invoice_bp.post("/<int:invoice_id>/restore")
@login_required
def invoice_restore(invoice_id: int):
    _require_invoice_delete_csrf()
    try:
        restore_deleted_invoice(invoice_id, restored_by=str(session.get("user") or ""))
        flash("請求書を復元しました。", "success")
    except InvoiceValidationError as exc:
        flash(str(exc), "warning")
    return redirect(url_for("invoice.invoice_deleted_list"))


@invoice_bp.post("/<int:invoice_id>/purge")
@login_required
def invoice_purge(invoice_id: int):
    _require_invoice_delete_csrf()
    try:
        invoice = purge_deleted_invoice(
            invoice_id,
            confirmed_invoice_no=request.form.get("confirm_invoice_no") or "",
            purged_by=str(session.get("user") or ""),
        )
        if _delete_stored_invoice_pdf(invoice.get("pdf_storage_path")):
            flash("請求書を完全削除しました。", "success")
        else:
            flash("請求書データは完全削除しましたが、保存PDFを削除できませんでした。", "warning")
    except InvoiceValidationError as exc:
        flash(str(exc), "warning")
    return redirect(url_for("invoice.invoice_deleted_list"))


@invoice_bp.get("/issuer-templates")
@login_required
def issuer_template_list():
    return render_template("issuer_templates.html", issuer_templates=list_issuer_templates())


@invoice_bp.route("/issuer-templates/new", methods=["GET", "POST"])
@login_required
def issuer_template_new():
    form_data = build_issuer_template_form_data()
    if request.method == "POST":
        form_data = build_issuer_template_form_data(request.form)
        try:
            template_id = create_issuer_template(request.form)
            flash("発行者テンプレートを登録しました。", "success")
            return redirect(url_for("invoice.issuer_template_edit", template_id=template_id))
        except InvoiceValidationError as exc:
            flash(str(exc), "warning")
    return render_template("issuer_template_form.html", form_data=form_data, mode="new")


@invoice_bp.route("/issuer-templates/<int:template_id>/edit", methods=["GET", "POST"])
@login_required
def issuer_template_edit(template_id: int):
    template = get_issuer_template_by_id(template_id)
    if not template:
        flash("発行者テンプレートが見つかりません。", "warning")
        return redirect(url_for("invoice.issuer_template_list"))

    form_data = build_issuer_template_form_data(template)
    if request.method == "POST":
        form_data = build_issuer_template_form_data(request.form)
        try:
            update_issuer_template(template_id, request.form)
            flash("発行者テンプレートを更新しました。", "success")
            return redirect(url_for("invoice.issuer_template_edit", template_id=template_id))
        except InvoiceValidationError as exc:
            flash(str(exc), "warning")
    return render_template(
        "issuer_template_form.html",
        form_data=form_data,
        mode="edit",
        issuer_template=template,
    )


@invoice_bp.post("/issuer-templates/<int:template_id>/delete")
@login_required
def issuer_template_delete(template_id: int):
    delete_issuer_template(template_id)
    flash("発行者テンプレートを削除しました。", "success")
    return redirect(url_for("invoice.issuer_template_list"))


@invoice_bp.post("/issuer-templates/<int:template_id>/default")
@login_required
def issuer_template_default(template_id: int):
    template = get_issuer_template_by_id(template_id)
    if not template:
        flash("発行者テンプレートが見つかりません。", "warning")
        return redirect(url_for("invoice.issuer_template_list"))
    set_default_issuer_template(template_id)
    flash("デフォルトテンプレートを更新しました。", "success")
    return redirect(url_for("invoice.issuer_template_list"))


@invoice_bp.get("/contacts")
@login_required
def contact_list():
    q = (request.args.get("q") or "").strip()
    contacts = fetch_contacts(q=q)
    return render_template("invoice_contact_list.html", contacts=contacts, q=q)


@invoice_bp.route("/contacts/new", methods=["GET", "POST"])
@login_required
def contact_new():
    if request.method == "POST":
        try:
            contact_id = save_contact(None, request.form)
            flash("請求先を登録しました。", "success")
            return redirect(url_for("invoice.contact_edit", contact_id=contact_id))
        except InvoiceValidationError as exc:
            flash(str(exc), "warning")
    return render_template("invoice_contact_form.html", form_data=request.form, mode="new")


@invoice_bp.route("/contacts/<int:contact_id>/edit", methods=["GET", "POST"])
@login_required
def contact_edit(contact_id: int):
    contact = get_contact(contact_id)
    if not contact:
        flash("請求先が見つかりません。", "warning")
        return redirect(url_for("invoice.contact_list"))
    if request.method == "POST":
        try:
            save_contact(contact_id, request.form)
            flash("請求先を更新しました。", "success")
            return redirect(url_for("invoice.contact_edit", contact_id=contact_id))
        except InvoiceValidationError as exc:
            flash(str(exc), "warning")
            contact = {**contact, **request.form}
    return render_template("invoice_contact_form.html", form_data=contact, mode="edit", contact=contact)


@invoice_bp.post("/contacts/<int:contact_id>/delete")
@login_required
def contact_delete(contact_id: int):
    delete_contact(contact_id)
    flash("請求先を削除しました。", "success")
    return redirect(url_for("invoice.contact_list"))


@invoice_bp.route("/new", methods=["GET", "POST"])
@login_required
def invoice_new():
    contacts = fetch_contacts()
    issuer_templates = list_issuer_templates()
    default_issuer_template = get_default_issuer_template()
    form_data = build_invoice_form_data()
    if request.method == "GET":
        if contacts:
            first_contact = contacts[0]
            form_data["contact_id"] = first_contact["id"]
            due_date = default_due_date(now_jst().date(), first_contact.get("default_due_days"))
            form_data["due_date"] = format_ymd(due_date)
            form_data["contact_email_snapshot"] = first_contact.get("email")
        if default_issuer_template:
            form_data = apply_issuer_template_to_form_data(form_data, default_issuer_template)
    if request.method == "POST":
        try:
            invoice_id = save_invoice(None, _normalized_invoice_form(request.form))
            flash("請求書を作成しました。", "success")
            return redirect(url_for("invoice.invoice_detail", invoice_id=invoice_id))
        except InvoiceValidationError as exc:
            flash(str(exc), "warning")
            form_data = _posted_invoice_form_data(request.form)
    return render_template(
        "invoice_form.html",
        form_data=form_data,
        contacts=contacts,
        issuer_templates=issuer_templates,
        fuel_cost_helper=build_fuel_cost_helper(),
        default_tax_category=DEFAULT_TAX_CATEGORY,
        tax_category_labels=TAX_CATEGORY_LABELS,
        tax_mode_labels=TAX_MODE_LABELS,
        status_labels=STATUS_LABELS,
        bank_info_mode_labels=BANK_INFO_MODE_LABELS,
        mode="new",
    )


@invoice_bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def invoice_edit(invoice_id: int):
    invoice = get_invoice(invoice_id)
    if not invoice:
        flash("請求書が見つかりません。", "warning")
        return redirect(url_for("invoice.invoice_list"))
    contacts = fetch_contacts()
    issuer_templates = list_issuer_templates()
    form_data = build_invoice_form_data(invoice)
    if request.method == "POST":
        try:
            save_invoice(invoice_id, _normalized_invoice_form(request.form))
            flash("請求書を更新しました。", "success")
            return redirect(url_for("invoice.invoice_detail", invoice_id=invoice_id))
        except InvoiceValidationError as exc:
            flash(str(exc), "warning")
            form_data = _posted_invoice_form_data(request.form, base=invoice)
    return render_template(
        "invoice_form.html",
        form_data=form_data,
        contacts=contacts,
        issuer_templates=issuer_templates,
        fuel_cost_helper=build_fuel_cost_helper(),
        default_tax_category=DEFAULT_TAX_CATEGORY,
        tax_category_labels=TAX_CATEGORY_LABELS,
        tax_mode_labels=TAX_MODE_LABELS,
        status_labels=STATUS_LABELS,
        bank_info_mode_labels=BANK_INFO_MODE_LABELS,
        invoice=invoice,
        mode="edit",
    )


@invoice_bp.post("/<int:invoice_id>/duplicate")
@login_required
def invoice_duplicate(invoice_id: int):
    try:
        new_id = duplicate_invoice(invoice_id)
        flash("請求書を複製しました。", "success")
        return redirect(url_for("invoice.invoice_detail", invoice_id=new_id))
    except InvoiceValidationError as exc:
        flash(str(exc), "warning")
        return redirect(url_for("invoice.invoice_detail", invoice_id=invoice_id))


@invoice_bp.get("/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id: int):
    invoice = get_invoice(invoice_id)
    if not invoice:
        flash("請求書が見つかりません。", "warning")
        return redirect(url_for("invoice.invoice_list"))
    card_payment_enabled = int(invoice.get("card_payment_enabled") or 0) == 1
    latest_card_payment = get_latest_invoice_card_payment(invoice_id)
    card_payment_status = (latest_card_payment or {}).get("square_status") or ""
    card_payment_url = None
    invoice_status = str(invoice.get("status") or "").strip().lower()
    if card_payment_enabled and invoice_status not in {"paid", "cancelled", "canceled"}:
        try:
            token = ensure_invoice_card_payment_token(invoice_id)
            card_payment_url = build_invoice_card_payment_url(token)
        except Exception:
            card_payment_url = None
    return render_template(
        "invoice_detail.html",
        invoice=invoice,
        freee_needs_resync=invoice_needs_freee_resync(invoice),
        effective_bank_info=get_invoice_effective_bank_info(invoice),
        status_labels=STATUS_LABELS,
        tax_mode_labels=TAX_MODE_LABELS,
        default_mail_subject=build_mail_subject(invoice.get("subject")),
        default_mail_body=build_default_invoice_mail_body(invoice),
        mail_to_default=invoice.get("contact_email_snapshot") or "",
        bank_info_mode_labels=BANK_INFO_MODE_LABELS,
        card_payment_enabled=card_payment_enabled,
        card_payment_url=card_payment_url,
        card_payment_status=card_payment_status,
        latest_card_payment=latest_card_payment,
    )


@invoice_bp.post("/<int:invoice_id>/freee-api")
@login_required
def invoice_freee_api_sync(invoice_id: int):
    invoice = get_invoice(invoice_id)
    if not invoice:
        flash("請求書が見つかりません。", "warning")
        return redirect(url_for("invoice.invoice_list"))
    contact = get_contact(invoice.get("contact_id")) if invoice.get("contact_id") else None
    try:
        result = sync_invoice_to_freee(invoice, contact)
        status = result.get("status")
        if status == "updated":
            flash("freee APIの取引を修正しました。", "success")
        elif status == "skipped_already_synced":
            flash("freee APIは反映済みです。", "info")
        else:
            flash("freee APIに取引を登録しました。", "success")
    except Exception as exc:
        flash(f"freee API登録に失敗しました: {exc}", "danger")
    return redirect(url_for("invoice.invoice_detail", invoice_id=invoice_id))


@invoice_bp.post("/<int:invoice_id>/issue")
@login_required
def invoice_issue(invoice_id: int):
    mark_invoice_issued(invoice_id)
    flash("請求書を発行済みに更新しました。", "success")
    return redirect(url_for("invoice.invoice_detail", invoice_id=invoice_id))


@invoice_bp.post("/<int:invoice_id>/status")
@login_required
def invoice_update_status(invoice_id: int):
    status = request.form.get("status") or "draft"
    before_invoice = get_invoice(invoice_id)
    before_status = str((before_invoice or {}).get("status") or "").strip().lower()
    update_invoice_status(invoice_id, status)
    normalized_status = str(status or "").strip().lower()
    if normalized_status == "paid" and (before_status != "paid" or not has_invoice_receipt_mail_sent(invoice_id)):
        try:
            invoice = get_invoice(invoice_id)
            if invoice and not has_invoice_receipt_mail_sent(invoice_id):
                to_email = (invoice.get("contact_email_snapshot") or "").strip()
                if to_email and int(invoice.get("total_yen") or 0) > 0:
                    latest_mail = get_latest_invoice_sent_mail_log(invoice_id) or {}
                    _, receipt_name, receipt_pdf = generate_invoice_receipt_pdf(
                        invoice,
                        paid_at=now_jst(),
                        payment_method="銀行振込",
                    )
                    send_invoice_receipt_mail(
                        invoice,
                        to_email=to_email,
                        cc_email=(latest_mail.get("cc_email") or None),
                        bcc_email=None,
                        reply_to_email=resolve_invoice_issuer_email(invoice) or None,
                        attachment_filename=receipt_name,
                        pdf_bytes=receipt_pdf,
                    )
                    flash("領収書PDFを送付しました。", "success")
                else:
                    flash("領収書メールは、宛先メールまたは金額が不足しているため送付しませんでした。", "warning")
        except Exception as exc:
            flash(f"ステータスは更新しましたが、領収書メール送付に失敗しました: {exc}", "danger")
    flash("ステータスを更新しました。", "success")
    return redirect(url_for("invoice.invoice_detail", invoice_id=invoice_id))


@invoice_bp.get("/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id: int):
    invoice = get_invoice(invoice_id)
    if not invoice:
        flash("請求書が見つかりません。", "warning")
        return redirect(url_for("invoice.invoice_list"))
    pdf_path, visible_name, _ = generate_invoice_pdf(invoice)
    return send_file(pdf_path, as_attachment=True, download_name=visible_name, mimetype="application/pdf")


def _resolve_invoice_mail_payment_links(invoice: dict, invoice_id: int) -> tuple[str | None, str | None]:
    normalized_bank_info_mode = normalize_bank_info_mode(invoice.get("bank_info_mode"))
    invoice_status = str(invoice.get("status") or "").strip().lower()
    card_payment_enabled = int(invoice.get("card_payment_enabled") or 0) == 1

    payout_access_url = None
    card_payment_url = None
    needs_payout_url = normalized_bank_info_mode == BANK_INFO_MODE_PAYOUT_LINK
    needs_card_url = card_payment_enabled and invoice_status not in {"paid", "cancelled", "canceled"}
    if needs_payout_url:
        try:
            payout_access = issue_payout_access_token_for_invoice(invoice)
            payout_access_url = payout_access.get("access_url") or ""
            save_invoice_payout_token(invoice_id, payout_access.get("id"))
            if not payout_access_url:
                raise RuntimeError("access_url が空です。")
        except Exception as exc:
            raise RuntimeError(f"振込先リンクの発行に失敗しました: {exc}") from exc
    if needs_card_url:
        try:
            card_token = ensure_invoice_card_payment_token(invoice_id)
            card_payment_url = build_invoice_card_payment_url(card_token)
            if not card_payment_url:
                raise RuntimeError("カード決済URLが空です。")
        except Exception as exc:
            raise RuntimeError(f"カード決済URLの発行に失敗しました: {exc}") from exc
    return payout_access_url, card_payment_url


def _build_invoice_mail_preview_context(invoice: dict, invoice_id: int, base_body: str) -> dict[str, str | None]:
    payout_access_url, card_payment_url = _resolve_invoice_mail_payment_links(invoice, invoice_id)
    preview_body = build_invoice_mail_body_with_payment_guidance(
        invoice=invoice,
        body=base_body,
        payout_access_url=payout_access_url,
        card_payment_url=card_payment_url,
    )
    return {
        "payout_access_url": payout_access_url,
        "card_payment_url": card_payment_url,
        "preview_body": preview_body,
    }


@invoice_bp.route("/<int:invoice_id>/mail", methods=["GET", "POST"])
@login_required
def invoice_mail(invoice_id: int):
    invoice = get_invoice(invoice_id)
    if not invoice:
        flash("請求書が見つかりません。", "warning")
        return redirect(url_for("invoice.invoice_list"))
    mail_context = dict(invoice)
    if invoice.get("contact_id"):
        contact = get_contact(int(invoice["contact_id"]))
        if contact:
            mail_context.update(
                {
                    "contact_name": contact.get("name"),
                    "contact_person": contact.get("contact_name"),
                    "honorific": contact.get("honorific"),
                }
            )
    effective_issuer_email = resolve_invoice_issuer_email(invoice)
    initial = {
        "to_email": invoice.get("contact_email_snapshot") or "",
        "cc_email": effective_issuer_email,
        "bcc_email": "",
        "subject": build_mail_subject(invoice.get("subject")),
        "body": build_default_invoice_mail_body(mail_context),
    }
    def _render_mail_form(form_data: dict, *, preview_context: dict | None = None):
        resolved_preview = preview_context or {}
        preview_body = resolved_preview.get("preview_body")
        if preview_body is None:
            preview_body = "プレビュー未生成です。プレビュー更新を実行してください。"
        return render_template(
            "invoice_mail_form.html",
            invoice=invoice,
            form_data=form_data,
            preview_body=preview_body,
            payout_access_url=resolved_preview.get("payout_access_url"),
            card_payment_url=resolved_preview.get("card_payment_url"),
        )

    if request.method == "POST":
        action = (request.form.get("action") or "send").strip().lower()
        to_email = (request.form.get("to_email") or "").strip()
        cc_email = (request.form.get("cc_email") or "").strip()
        final_cc_email = merge_invoice_cc_emails(effective_issuer_email, cc_email)
        bcc_email = (request.form.get("bcc_email") or "").strip()
        subject = (request.form.get("subject") or "").strip()
        body = request.form.get("body") or ""
        form_data = dict(request.form)
        form_data["body"] = body
        try:
            preview_context = _build_invoice_mail_preview_context(invoice, invoice_id, body)
        except Exception as exc:
            flash(str(exc), "danger")
            return _render_mail_form(form_data)

        if action == "preview":
            return _render_mail_form(form_data, preview_context=preview_context)

        if not to_email:
            flash("宛先メールアドレスを入力してください。", "warning")
            return _render_mail_form(form_data, preview_context=preview_context)
        if request.form.get("confirm_send") != "yes":
            flash("送信前確認にチェックを入れてください。", "warning")
            return _render_mail_form(form_data, preview_context=preview_context)
        final_body = preview_context["preview_body"]
        try:
            _, attachment_name, pdf_bytes = generate_invoice_pdf(invoice)
            send_invoice_mail(
                invoice,
                to_email=to_email,
                cc_email=final_cc_email or None,
                bcc_email=bcc_email or None,
                reply_to_email=effective_issuer_email or None,
                subject=subject,
                body=final_body,
                attachment_filename=attachment_name,
                pdf_bytes=pdf_bytes,
            )
            flash("請求書メールを送信しました。", "success")
            return redirect(url_for("invoice.invoice_detail", invoice_id=invoice_id))
        except Exception as exc:
            flash(f"メール送信に失敗しました: {exc}", "danger")
            form_data["body"] = body
            return _render_mail_form(form_data, preview_context=preview_context)
    try:
        preview_context = _build_invoice_mail_preview_context(invoice, invoice_id, initial["body"])
    except Exception as exc:
        flash(str(exc), "danger")
        preview_context = None
    return _render_mail_form(initial, preview_context=preview_context)


@invoice_bp.get("/<int:invoice_id>/freee-csv")
@login_required
def invoice_freee_csv(invoice_id: int):
    invoice = get_invoice(invoice_id)
    if not invoice:
        flash("請求書が見つかりません。", "warning")
        return redirect(url_for("invoice.invoice_list"))
    contact = get_contact(invoice.get("contact_id")) if invoice.get("contact_id") else None
    invoice["freee_partner_name"] = (contact or {}).get("freee_partner_name")
    try:
        csv_bytes, filename = build_invoice_freee_csv(invoice)
        log_csv_export(invoice_id, filename, status="success")
        response = build_invoice_freee_csv_response(invoice)
        return response
    except Exception as exc:
        log_csv_export(invoice_id, f"freee_invoice_{invoice.get('invoice_no')}.csv", status="failed", error_message=str(exc))
        flash(f"freee CSV の出力に失敗しました: {exc}", "danger")
        return redirect(url_for("invoice.invoice_detail", invoice_id=invoice_id))


@invoice_bp.get("/<int:invoice_id>/mail/new")
@login_required
def invoice_mail_redirect(invoice_id: int):
    return redirect(url_for("invoice.invoice_mail", invoice_id=invoice_id))


def _card_payment_invoice_error(invoice: dict | None) -> tuple[dict | None, str | None]:
    if not invoice:
        return None, "無効な決済URLです。"
    if int(invoice.get("card_payment_enabled") or 0) != 1:
        return None, "この請求書はカード決済に対応していません。"
    invoice_status = str(invoice.get("status") or "").strip().lower()
    if invoice_status in {"paid", "cancelled", "canceled"}:
        return None, "現在この請求書はカード決済を受け付けていません。"
    if int(invoice.get("total_yen") or 0) <= 0:
        return None, "決済金額が不正です。"
    return invoice, None


def _send_invoice_receipt_mail_if_needed(
    invoice_id: int,
    *,
    paid_at=None,
    payment_method: str = "銀行振込",
) -> bool:
    invoice = get_invoice(invoice_id)
    if not invoice or has_invoice_receipt_mail_sent(invoice_id):
        return False
    to_email = (invoice.get("contact_email_snapshot") or "").strip()
    if not to_email or int(invoice.get("total_yen") or 0) <= 0:
        return False
    latest_mail = get_latest_invoice_sent_mail_log(invoice_id) or {}
    _, receipt_name, receipt_pdf = generate_invoice_receipt_pdf(
        invoice,
        paid_at=paid_at or now_jst(),
        payment_method=payment_method,
    )
    send_invoice_receipt_mail(
        invoice,
        to_email=to_email,
        cc_email=(latest_mail.get("cc_email") or None),
        bcc_email=None,
        reply_to_email=resolve_invoice_issuer_email(invoice) or None,
        attachment_filename=receipt_name,
        pdf_bytes=receipt_pdf,
    )
    return True


@invoice_bp.get("/pay/<token>")
def invoice_card_payment_page(token: str):
    invoice = get_invoice_by_card_payment_token(token)
    invoice, err = _card_payment_invoice_error(invoice)
    if err:
        return render_template("invoice_card_pay_invalid.html", message=err), 404
    square = get_invoice_square_config()
    if not square.get("application_id") or not square.get("location_id"):
        return render_template("invoice_card_pay_invalid.html", message="カード決済の設定が未完了です。"), 400
    buyer_email = (invoice.get("contact_email_snapshot") or "").strip()
    checkout_key = f"invoice:{token}:{int(invoice.get('total_yen') or 0)}"
    return render_template(
        "pay.html",
        checkout_mode="invoice",
        invoice=invoice,
        token=token,
        event={"uuid": token, "title": "請求書決済"},
        event_amount=int(invoice.get("total_yen") or 0),
        autofill={},
        payment_token="",
        return_url="",
        force_square_card=False,
        is_tip_payment=False,
        square_js_url=square.get("js_url"),
        app_id=square.get("application_id"),
        location_id=square.get("location_id"),
        checkout_email_masked=mask_email(buyer_email),
        otp_verified=bool(
            buyer_email
            and is_checkout_otp_verified(checkout_type="invoice", checkout_key=checkout_key, email=buyer_email)
        ),
        otp_send_url=url_for("invoice.invoice_card_otp_send", token=token),
        otp_verify_url=url_for("invoice.invoice_card_otp_verify", token=token),
    )


def _invoice_checkout_otp_context(token: str) -> tuple[dict, str, str]:
    invoice = get_invoice_by_card_payment_token(token)
    invoice, err = _card_payment_invoice_error(invoice)
    if err:
        raise CheckoutOtpError(err, 400, "invalid_invoice")
    buyer_email = (invoice.get("contact_email_snapshot") or "").strip()
    if not buyer_email:
        raise CheckoutOtpError("請求先メールアドレスが未設定です。", 400, "missing_email")
    return invoice, f"invoice:{token}:{int(invoice.get('total_yen') or 0)}", buyer_email


def _invoice_otp_json_error(exc: CheckoutOtpError):
    return jsonify(ok=False, error=exc.error_code, message=exc.message), exc.status_code


@invoice_bp.post("/api/pay/<token>/otp/send")
def invoice_card_otp_send(token: str):
    try:
        invoice, checkout_key, buyer_email = _invoice_checkout_otp_context(token)
        result = send_checkout_otp(checkout_type="invoice", checkout_key=checkout_key, email=buyer_email)
        return jsonify(ok=True, **result)
    except CheckoutOtpError as exc:
        return _invoice_otp_json_error(exc)


@invoice_bp.post("/api/pay/<token>/otp/verify")
def invoice_card_otp_verify(token: str):
    data = request.get_json(silent=True) or {}
    try:
        _invoice, checkout_key, buyer_email = _invoice_checkout_otp_context(token)
        result = verify_checkout_otp(
            checkout_type="invoice",
            checkout_key=checkout_key,
            email=buyer_email,
            code=data.get("code"),
        )
        result["billing_contact"] = {
            "email": buyer_email,
            "givenName": (_invoice.get("contact_name_snapshot") or _invoice.get("contact_person_snapshot") or "").strip(),
            "countryCode": "JP",
        }
        return jsonify(ok=True, **result)
    except CheckoutOtpError as exc:
        return _invoice_otp_json_error(exc)


@invoice_bp.post("/api/pay/<token>/precheck")
def invoice_card_precheck(token: str):
    invoice = get_invoice_by_card_payment_token(token)
    invoice, err = _card_payment_invoice_error(invoice)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify(
        {
            "ok": True,
            "invoice_no": invoice.get("invoice_no"),
            "subject": invoice.get("subject"),
            "amount_yen": int(invoice.get("total_yen") or 0),
            "status": invoice.get("status"),
        }
    )


@invoice_bp.post("/api/pay/<token>/charge")
def invoice_card_charge(token: str):
    data = request.get_json(silent=True) or {}
    source_id = data.get("sourceId")
    posted_buyer_name = data.get("buyer_name")
    _wt_in = data.get("walletType", data.get("wallet_type"))
    if isinstance(_wt_in, str):
        _wt_in = _wt_in.strip().upper()
    else:
        _wt_in = ""
    wallet_type = _wt_in if _wt_in in ("APPLE_PAY", "GOOGLE_PAY") else None
    if not source_id:
        return jsonify({"ok": False, "error": "sourceId は必須です。"}), 400
    invoice = get_invoice_by_card_payment_token(token)
    invoice, err = _card_payment_invoice_error(invoice)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    buyer_email = (invoice.get("contact_email_snapshot") or "").strip()
    if not buyer_email:
        return jsonify({"ok": False, "error": "請求先メールアドレスが未設定のため決済を開始できません。"}), 400
    checkout_key = f"invoice:{token}:{int(invoice.get('total_yen') or 0)}"
    try:
        require_checkout_otp(checkout_type="invoice", checkout_key=checkout_key, email=buyer_email)
    except CheckoutOtpError as exc:
        return jsonify({"ok": False, "error": exc.message, "error_code": exc.error_code}), exc.status_code

    square = get_invoice_square_config()
    access_token = square.get("access_token")
    location_id = square.get("location_id")
    if not access_token or not location_id:
        return jsonify({"ok": False, "error": "Square設定が未完了です。"}), 500

    buyer_name = (
        (invoice.get("contact_name_snapshot") or "").strip()
        or (invoice.get("contact_person_snapshot") or "").strip()
        or (posted_buyer_name or "").strip()
        or "(不明)"
    )
    pending = create_invoice_card_payment_pending(invoice, buyer_name=buyer_name, wallet_type=wallet_type)
    idempotency_key = pending["idempotency_key"]
    payment_row_id = int(pending["id"])
    try:
        customer_id = ensure_invoice_square_customer(access_token=access_token, invoice=invoice, buyer_name=buyer_name)
    except Exception:
        customer_id = None
        current_app.logger.exception("invoice_card_charge: Square customer creation failed; continuing without customer_id")
    body = {
        "idempotency_key": idempotency_key,
        "source_id": source_id,
        "amount_money": {"amount": int(invoice.get("total_yen") or 0), "currency": "JPY"},
        "location_id": location_id,
        "reference_id": f"invoice:{invoice.get('id')}:pay:{payment_row_id}",
        "buyer_email_address": buyer_email,
        "customer_details": {
            "customer_initiated": True,
            "seller_keyed_in": False,
        },
    }
    if customer_id:
        body["customer_id"] = customer_id
    try:
        resp = request_square(
            "POST",
            f"{square['api_base']}/v2/payments",
            access_token=access_token,
            json_body=body,
            timeout=25,
            idempotency_key=idempotency_key,
        )
        payload = resp.json() if resp.content else {}
    except SquareTransportError as exc:
        update_invoice_card_payment_result(
            payment_row_id,
            status="UNKNOWN",
            error_code="TRANSPORT_UNKNOWN",
            error_detail=str(exc),
            sync_error=str(exc),
        )
        return jsonify({
            "ok": False,
            "error": "決済結果を確認中です。再度支払わず、管理者へお問い合わせください。",
            "error_code": "PAYMENT_RESULT_UNKNOWN",
            "payment_row_id": payment_row_id,
        }), 503

    if resp.status_code >= 400:
        errors = payload.get("errors") or []
        error_info = square_error_info(resp)
        uncertain = resp.status_code >= 500
        update_invoice_card_payment_result(
            payment_row_id,
            status="UNKNOWN" if uncertain else "FAILED",
            error_code=error_info.code,
            error_detail=error_info.detail,
            sync_error=error_info.detail if uncertain else None,
        )
        return jsonify({
            "ok": False,
            "error": "決済結果を確認中です。再度支払わず、管理者へお問い合わせください。" if uncertain else (error_info.detail or "Square API error"),
            "errors": errors,
        }), 503 if uncertain else 400

    payment = payload.get("payment") or {}
    card = ((payment.get("card_details") or {}).get("card") or {})
    status = normalize_square_status(payment.get("status"), default="UNKNOWN")
    paid_at = now_jst() if is_payment_completed(status) else None
    update_invoice_card_payment_result(
        payment_row_id,
        status=status,
        square_payment_id=payment.get("id"),
        receipt_url=payment.get("receipt_url"),
        card_brand=card.get("card_brand"),
        card_last4=card.get("last_4"),
        card_exp_mm=card.get("exp_month"),
        card_exp_yyyy=card.get("exp_year"),
        error_code=None,
        error_detail=None,
        paid_at=paid_at,
        square_updated_at=square_datetime(payment.get("updated_at")),
        sync_error=None,
    )
    if is_payment_completed(status):
        mark_invoice_paid_by_card(int(invoice.get("id")), paid_at=paid_at)
        notify_invoice_card_payment_if_needed(payment_row_id)
        try:
            _send_invoice_receipt_mail_if_needed(
                int(invoice.get("id")),
                paid_at=paid_at,
                payment_method="クレジットカード決済",
            )
        except Exception:
            current_app.logger.exception("invoice receipt mail failed after card charge invoice_id=%s", invoice.get("id"))

        consume_checkout_otp(checkout_type="invoice", checkout_key=checkout_key, email=buyer_email)

    return jsonify(
        {
            "ok": True,
            "payment_id": payment.get("id"),
            "status": payment.get("status"),
            "receipt_url": payment.get("receipt_url"),
            "thanks_url": url_for("invoice.invoice_card_thanks", token=token, _external=True),
            "processing": not is_payment_completed(status),
        }
    )


@invoice_bp.get("/pay/<token>/thanks")
def invoice_card_thanks(token: str):
    invoice = get_invoice_by_card_payment_token(token)
    if not invoice:
        return render_template("invoice_card_pay_invalid.html", message="無効な決済URLです。"), 404
    latest = get_latest_invoice_card_payment(int(invoice.get("id")))
    return render_template("invoice_card_pay_thanks.html", invoice=invoice, latest_card_payment=latest)


def _invoice_payment_reference_ids(reference_id: str | None) -> tuple[int | None, int | None]:
    if not reference_id:
        return None, None
    parts = str(reference_id).split(":")
    if len(parts) != 4 or parts[0] != "invoice" or parts[2] != "pay":
        return None, None
    try:
        invoice_id = int(parts[1])
        payment_row_id = int(parts[3])
        return (invoice_id if invoice_id > 0 else None, payment_row_id if payment_row_id > 0 else None)
    except (TypeError, ValueError):
        return None, None


def _invoice_record_is_square_managed(record: dict) -> bool:
    created_at = record.get("created_at")
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT managed_from FROM square_sync_control WHERE id=1 LIMIT 1")
        row = cur.fetchone() or {}
        managed_from = row.get("managed_from")
        return bool(created_at and managed_from and created_at >= managed_from)
    finally:
        cur.close()
        db.close()


def _register_invoice_square_webhook(event: dict, raw_body: str, object_id: str | None) -> tuple[int | None, bool]:
    raw_event_id = str(event.get("event_id") or "").strip()
    event_id = f"invoice:{raw_event_id}" if raw_event_id else ""
    event_type = str(event.get("type") or "").strip()
    if not event_id or not event_type:
        return None, False
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            INSERT IGNORE INTO square_webhook_events
              (square_event_id, event_type, object_id, payload_sha256, processing_status)
            VALUES (%s,%s,%s,%s,'RECEIVED')
        """, (event_id, event_type, object_id, hashlib.sha256(raw_body.encode("utf-8")).hexdigest()))
        if cur.rowcount:
            row_id = int(cur.lastrowid)
            db.commit()
            return row_id, True
        cur.execute("SELECT id, processing_status FROM square_webhook_events WHERE square_event_id=%s LIMIT 1", (event_id,))
        row = cur.fetchone()
        if row and row.get("processing_status") == "FAILED":
            cur.execute("""
                UPDATE square_webhook_events
                   SET processing_status='RECEIVED', error_detail=NULL, processed_at=NULL
                 WHERE id=%s AND processing_status='FAILED'
            """, (row["id"],))
            can_retry = bool(cur.rowcount)
            db.commit()
            return int(row["id"]), can_retry
        return (int(row["id"]) if row else None), False
    finally:
        cur.close()
        db.close()


def _finish_invoice_square_webhook(webhook_row_id: int | None, *, status: str, error: str | None = None) -> None:
    if not webhook_row_id:
        return
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            UPDATE square_webhook_events
               SET processing_status=%s, error_detail=%s, processed_at=NOW()
             WHERE id=%s
        """, (status, error or None, webhook_row_id))
        db.commit()
    finally:
        cur.close()
        db.close()


@invoice_bp.post("/webhooks/card")
def invoice_card_webhook():
    square = get_invoice_square_config()
    sig_key = square.get("webhook_signature_key")
    if not sig_key:
        current_app.logger.critical("Square invoice webhook signature key is missing env=%s", square.get("env"))
        return "webhook signature key is not configured", 503
    raw_body = request.get_data(as_text=True)
    try:
        from square.utilities.webhooks_helper import is_valid_webhook_event_signature

        sig_header = request.headers.get("x-square-hmacsha256-signature", "")
        webhook_url = f"{os.environ.get('MFU_PUBLIC_BASE_URL', 'https://mfu.iori0624.jp').rstrip('/')}/invoice/webhooks/card"
        if not is_valid_webhook_event_signature(raw_body, sig_header, sig_key, webhook_url):
            return "invalid signature", 403
    except Exception:
        current_app.logger.exception("invoice webhook signature validation failed")
        return "signature check failed", 403

    ev = request.get_json(silent=True) or {}
    if ev.get("type") != "payment.updated":
        return "", 200
    payment = (((ev.get("data") or {}).get("object") or {}).get("payment") or {})
    square_payment_id = payment.get("id")
    if not square_payment_id:
        return "", 200
    _ensure_schema_once()
    webhook_row_id, should_process = _register_invoice_square_webhook(ev, raw_body, square_payment_id)
    if not should_process:
        return "", 200
    try:
        record = get_invoice_card_payment_by_square_payment_id(square_payment_id)
        ref_invoice_id, ref_payment_row_id = _invoice_payment_reference_ids(payment.get("reference_id"))
        if not record and ref_payment_row_id:
            candidate = get_invoice_card_payment_by_id(ref_payment_row_id)
            if candidate and int(candidate.get("invoice_id") or 0) == int(ref_invoice_id or 0):
                record = candidate
        if not record or not _invoice_record_is_square_managed(record):
            _finish_invoice_square_webhook(webhook_row_id, status="IGNORED", error="legacy_or_unknown_invoice_payment")
            return "", 200
        if not should_apply_square_update(record.get("square_updated_at"), payment.get("updated_at")):
            _finish_invoice_square_webhook(webhook_row_id, status="IGNORED", error="older_payment_update")
            return "", 200

        card = ((payment.get("card_details") or {}).get("card") or {})
        status = normalize_square_status(payment.get("status"), default="UNKNOWN")
        paid_at = now_jst() if is_payment_completed(status) else None
        update_invoice_card_payment_result(
            int(record.get("id")),
            status=status,
            square_payment_id=square_payment_id,
            receipt_url=payment.get("receipt_url"),
            card_brand=card.get("card_brand"),
            card_last4=card.get("last_4"),
            card_exp_mm=card.get("exp_month"),
            card_exp_yyyy=card.get("exp_year"),
            paid_at=paid_at,
            square_updated_at=square_datetime(payment.get("updated_at")),
            sync_error=None,
        )
        if is_payment_completed(status):
            mark_invoice_paid_by_card(int(record.get("invoice_id")), paid_at=paid_at)
            notify_invoice_card_payment_if_needed(int(record.get("id")))
            try:
                _send_invoice_receipt_mail_if_needed(
                    int(record.get("invoice_id")),
                    paid_at=paid_at,
                    payment_method="クレジットカード決済",
                )
            except Exception:
                current_app.logger.exception(
                    "invoice receipt mail failed from card webhook invoice_id=%s payment_row_id=%s",
                    record.get("invoice_id"),
                    record.get("id"),
                )
        _finish_invoice_square_webhook(webhook_row_id, status="PROCESSED")
    except Exception as exc:
        current_app.logger.exception("invoice Square webhook processing failed")
        _finish_invoice_square_webhook(webhook_row_id, status="FAILED", error=str(exc)[:1000])
        return "webhook processing failed", 500
    return "", 200


def _normalized_invoice_form(form):
    return _FormProxy({
        "contact_id": form.get("contact_id"),
        "issue_date": parse_date(form.get("issue_date")),
        "due_date": parse_date(form.get("due_date")),
        "subject": form.get("subject"),
        "note": form.get("note"),
        "bank_info": form.get("bank_info"),
        "bank_info_mode": form.get("bank_info_mode"),
        "card_payment_enabled": form.get("card_payment_enabled"),
        "issuer_template_id": form.get("issuer_template_id"),
        "issuer_name": form.get("issuer_name"),
        "issuer_postal_code": form.get("issuer_postal_code"),
        "issuer_address1": form.get("issuer_address1"),
        "issuer_address2": form.get("issuer_address2"),
        "issuer_phone": form.get("issuer_phone"),
        "issuer_email": form.get("issuer_email"),
        "tax_mode": form.get("tax_mode"),
        "status": form.get("status") or "draft",
        "getlist": form.getlist,
    })


class _FormProxy(dict):
    def getlist(self, key):
        getter = super().get("getlist")
        if callable(getter):
            return getter(key)
        value = super().get(key, [])
        return value if isinstance(value, list) else [value]



def _posted_invoice_form_data(form, base=None):
    base = base or {}
    data = dict(base)
    data.update({
        "contact_id": form.get("contact_id"),
        "issue_date": form.get("issue_date"),
        "due_date": form.get("due_date"),
        "subject": form.get("subject"),
        "note": form.get("note"),
        "bank_info": form.get("bank_info"),
        "bank_info_mode": form.get("bank_info_mode"),
        "card_payment_enabled": form.get("card_payment_enabled") or "0",
        "issuer_template_id": form.get("issuer_template_id"),
        "issuer_name": form.get("issuer_name"),
        "issuer_postal_code": form.get("issuer_postal_code"),
        "issuer_address1": form.get("issuer_address1"),
        "issuer_address2": form.get("issuer_address2"),
        "issuer_phone": form.get("issuer_phone"),
        "issuer_email": form.get("issuer_email"),
        "tax_mode": form.get("tax_mode"),
        "status": form.get("status") or "draft",
    })
    proxy = _FormProxy({"getlist": form.getlist})
    try:
        items = parse_invoice_items(proxy)
        data["items"] = [
            {
                "row_type": item.row_type,
                "item_name": item.item_name,
                "memo_text": item.memo_text,
                "quantity": str(item.quantity),
                "unit_name": item.unit_name,
                "unit_price_yen": str(item.unit_price_yen),
                "line_total_yen": str(item.line_total_yen),
                "tax_category": item.tax_category,
            }
            for item in items
        ]
    except Exception:
        row_types = form.getlist("row_type[]")
        item_names = form.getlist("item_name[]")
        memo_texts = form.getlist("memo_text[]")
        quantities = form.getlist("quantity[]")
        unit_names = form.getlist("unit_name[]")
        unit_prices = form.getlist("unit_price_yen[]")
        tax_categories = form.getlist("tax_category[]")
        row_count = max(
            len(row_types),
            len(item_names),
            len(memo_texts),
            len(quantities),
            len(unit_names),
            len(unit_prices),
            len(tax_categories),
        )
        data["items"] = [
            {
                "row_type": (row_types[index] if index < len(row_types) else ROW_TYPE_NORMAL) or ROW_TYPE_NORMAL,
                "item_name": item_names[index] if index < len(item_names) else "",
                "memo_text": memo_texts[index] if index < len(memo_texts) else "",
                "quantity": quantities[index] if index < len(quantities) else "",
                "unit_name": unit_names[index] if index < len(unit_names) else "",
                "unit_price_yen": unit_prices[index] if index < len(unit_prices) else "",
                "line_total_yen": "0",
                "tax_category": (tax_categories[index] if index < len(tax_categories) else DEFAULT_TAX_CATEGORY) or DEFAULT_TAX_CATEGORY,
            }
            for index in range(row_count)
        ] or [{
            "row_type": ROW_TYPE_NORMAL,
            "item_name": "",
            "memo_text": "",
            "quantity": "1.00",
            "unit_name": "式",
            "unit_price_yen": "0",
            "line_total_yen": "0",
            "tax_category": DEFAULT_TAX_CATEGORY,
        }]
    return data
