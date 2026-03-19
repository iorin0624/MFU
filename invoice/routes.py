from __future__ import annotations

import threading
from functools import wraps

from flask import flash, redirect, render_template, request, send_file, session, url_for

from . import invoice_bp
from .freee_csv import build_invoice_freee_csv_response, build_invoice_freee_csv
from .mail import send_invoice_mail
from .payout_client import InvoicePayoutClientError, create_invoice_payout_access
from .pdf import generate_invoice_pdf
from .services import (
    BANK_INFO_MODE_LABELS,
    BANK_INFO_MODE_PAYOUT_LINK,
    InvoiceValidationError,
    apply_issuer_template_to_form_data,
    append_payout_guidance_to_mail_body,
    build_default_invoice_mail_body,
    build_issuer_template_form_data,
    build_fuel_cost_helper,
    build_invoice_form_data,
    create_issuer_template,
    delete_contact,
    delete_issuer_template,
    duplicate_invoice,
    ensure_invoice_schema,
    fetch_contacts,
    get_default_issuer_template,
    get_contact,
    get_invoice_effective_bank_info,
    get_invoice,
    get_issuer_template_by_id,
    list_invoices,
    list_issuer_templates,
    log_csv_export,
    mark_invoice_issued,
    merge_invoice_cc_emails,
    parse_invoice_items,
    resolve_invoice_issuer_email,
    save_contact,
    save_invoice,
    save_invoice_payout_token,
    set_default_issuer_template,
    update_issuer_template,
    update_invoice_status,
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


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            flash("ログインが必要です。", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


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
    )


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
    return render_template(
        "invoice_detail.html",
        invoice=invoice,
        effective_bank_info=get_invoice_effective_bank_info(invoice),
        status_labels=STATUS_LABELS,
        tax_mode_labels=TAX_MODE_LABELS,
        default_mail_subject=build_mail_subject(invoice.get("issue_date")),
        default_mail_body=build_default_invoice_mail_body(invoice),
        mail_to_default=invoice.get("contact_email_snapshot") or "",
        bank_info_mode_labels=BANK_INFO_MODE_LABELS,
    )


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
    update_invoice_status(invoice_id, status)
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
        "subject": build_mail_subject(invoice.get("issue_date")),
        "body": build_default_invoice_mail_body(mail_context),
    }
    if request.method == "POST":
        to_email = (request.form.get("to_email") or "").strip()
        cc_email = (request.form.get("cc_email") or "").strip()
        final_cc_email = merge_invoice_cc_emails(effective_issuer_email, cc_email)
        bcc_email = (request.form.get("bcc_email") or "").strip()
        subject = (request.form.get("subject") or "").strip()
        body = request.form.get("body") or ""
        final_body = body
        if not to_email:
            flash("宛先メールアドレスを入力してください。", "warning")
            return render_template("invoice_mail_form.html", invoice=invoice, form_data=request.form)
        if request.form.get("confirm_send") != "yes":
            flash("送信前確認にチェックを入れてください。", "warning")
            return render_template("invoice_mail_form.html", invoice=invoice, form_data=request.form)
        try:
            if invoice.get("bank_info_mode") == BANK_INFO_MODE_PAYOUT_LINK:
                payout_access = create_invoice_payout_access(invoice)
                final_body = append_payout_guidance_to_mail_body(body, payout_access.get("access_url") or "")
                save_invoice_payout_token(invoice_id, payout_access.get("token_id"))
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
        except InvoicePayoutClientError:
            flash("振込先リンクの発行に失敗したためメール送信を中止しました。", "danger")
            form_data = dict(request.form)
            form_data["body"] = body
            return render_template("invoice_mail_form.html", invoice=invoice, form_data=form_data)
        except Exception as exc:
            flash(f"メール送信に失敗しました: {exc}", "danger")
            form_data = dict(request.form)
            form_data["body"] = final_body
            return render_template("invoice_mail_form.html", invoice=invoice, form_data=form_data)
    return render_template("invoice_mail_form.html", invoice=invoice, form_data=initial)


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


def _normalized_invoice_form(form):
    return _FormProxy({
        "contact_id": form.get("contact_id"),
        "issue_date": parse_date(form.get("issue_date")),
        "due_date": parse_date(form.get("due_date")),
        "subject": form.get("subject"),
        "note": form.get("note"),
        "bank_info": form.get("bank_info"),
        "bank_info_mode": form.get("bank_info_mode"),
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
