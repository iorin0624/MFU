from __future__ import annotations

import os
import time
from io import BytesIO
from functools import wraps

from flask import abort, current_app, flash, g, redirect, render_template, request, send_file, session, url_for

from . import invoice_bp
from .received_services import (
    GRANT_SECONDS,
    RECEIVED_STATUSES,
    ReceivedInvoiceError,
    admin_form_from_request,
    admin_action,
    create_received_request,
    create_guidance_template,
    default_received_form,
    delete_guidance_template,
    ensure_received_invoice_schema,
    generate_received_pdf,
    get_received_request,
    get_guidance_template,
    list_guidance_templates,
    list_received_requests,
    mask_email,
    notify_submitted,
    record_event,
    render_received_pdf_bytes,
    send_received_otp,
    send_request_invitation,
    set_default_guidance_template,
    update_recipient_draft,
    update_received_request_admin,
    update_guidance_template,
    verify_received_otp,
)


def _guidance_template_data(rows: list[dict]) -> list[dict]:
    return [
        {"id": int(row["id"]), "name": row["template_name"], "body": row["body"], "is_default": bool(row["is_default"])}
        for row in rows
    ]


def _admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user"):
            flash("ログインが必要です。", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def _load_public(public_id: str) -> dict:
    ensure_received_invoice_schema()
    row = get_received_request(public_id=public_id)
    if not row:
        abort(404)
    if row.get("expires_at") and row["expires_at"].timestamp() < time.time() and row["status"] in {"sent", "editing", "revision_requested"}:
        row["status"] = "expired"
    return row


def _grant_key(public_id: str) -> str:
    return f"received_invoice_grant:{public_id}"


def _is_granted(row: dict) -> bool:
    grant = session.get(_grant_key(row["public_id"])) or {}
    try:
        return int(grant.get("version") or 0) == int(row["auth_version"]) and float(grant.get("until") or 0) >= time.time()
    except (TypeError, ValueError):
        return False


def _require_grant(row: dict) -> None:
    if not _is_granted(row):
        raise ReceivedInvoiceError("メール認証の有効期限が切れました。もう一度認証してください。", status=401)


@invoice_bp.get("/received")
@_admin_required
def received_invoice_list():
    ensure_received_invoice_schema()
    status = str(request.args.get("status") or "").strip()
    q = str(request.args.get("q") or "").strip()
    return render_template(
        "received_invoice_list.html",
        invoices=list_received_requests(status, q),
        statuses=RECEIVED_STATUSES,
        status=status,
        q=q,
    )


@invoice_bp.route("/received/new", methods=["GET", "POST"])
@_admin_required
def received_invoice_new():
    ensure_received_invoice_schema()
    guidance_templates = list_guidance_templates()
    form_data = default_received_form()
    if request.method == "POST":
        form_data = admin_form_from_request(request.form)
        try:
            request_id = create_received_request(request.form, request.remote_addr or "")
            flash("請求書作成依頼の素案を保存しました。", "success")
            return redirect(url_for("invoice.received_invoice_detail", request_id=request_id))
        except ReceivedInvoiceError as exc:
            flash(str(exc), "warning")
    return render_template(
        "received_invoice_form.html",
        form_data=form_data,
        guidance_templates=guidance_templates,
        guidance_template_data=_guidance_template_data(guidance_templates),
    )


@invoice_bp.route("/received/<int:request_id>/edit", methods=["GET", "POST"])
@_admin_required
def received_invoice_edit(request_id: int):
    ensure_received_invoice_schema()
    guidance_templates = list_guidance_templates()
    row = get_received_request(request_id=request_id)
    if not row:
        abort(404)
    if request.method == "POST":
        try:
            update_received_request_admin(row, request.form, request.remote_addr or "")
            flash("素案を更新しました。", "success")
            return redirect(url_for("invoice.received_invoice_detail", request_id=request_id))
        except ReceivedInvoiceError as exc:
            flash(str(exc), "warning")
            row = {**row, **admin_form_from_request(request.form)}
    return render_template(
        "received_invoice_form.html",
        form_data=row,
        invoice=row,
        guidance_templates=guidance_templates,
        guidance_template_data=_guidance_template_data(guidance_templates),
    )


@invoice_bp.get("/received/guidance-templates")
@_admin_required
def received_guidance_template_list():
    ensure_received_invoice_schema()
    return render_template("received_guidance_templates.html", templates=list_guidance_templates())


@invoice_bp.route("/received/guidance-templates/new", methods=["GET", "POST"])
@_admin_required
def received_guidance_template_new():
    ensure_received_invoice_schema()
    form_data = {"template_name": "", "body": "", "sort_order": 0, "is_default": False}
    if request.method == "POST":
        form_data = dict(request.form)
        try:
            create_guidance_template(request.form)
            flash("案内テンプレートを登録しました。", "success")
            return redirect(url_for("invoice.received_guidance_template_list"))
        except ReceivedInvoiceError as exc:
            flash(str(exc), "warning")
    return render_template("received_guidance_template_form.html", form_data=form_data, mode="new")


@invoice_bp.route("/received/guidance-templates/<int:template_id>/edit", methods=["GET", "POST"])
@_admin_required
def received_guidance_template_edit(template_id: int):
    ensure_received_invoice_schema()
    template = get_guidance_template(template_id)
    if not template:
        flash("案内テンプレートが見つかりません。", "warning")
        return redirect(url_for("invoice.received_guidance_template_list"))
    form_data = template
    if request.method == "POST":
        form_data = dict(request.form)
        try:
            update_guidance_template(template_id, request.form)
            flash("案内テンプレートを更新しました。", "success")
            return redirect(url_for("invoice.received_guidance_template_list"))
        except ReceivedInvoiceError as exc:
            flash(str(exc), "warning")
    return render_template("received_guidance_template_form.html", form_data=form_data, mode="edit", template=template)


@invoice_bp.post("/received/guidance-templates/<int:template_id>/delete")
@_admin_required
def received_guidance_template_delete(template_id: int):
    ensure_received_invoice_schema()
    delete_guidance_template(template_id)
    flash("案内テンプレートを削除しました。", "success")
    return redirect(url_for("invoice.received_guidance_template_list"))


@invoice_bp.post("/received/guidance-templates/<int:template_id>/default")
@_admin_required
def received_guidance_template_default(template_id: int):
    ensure_received_invoice_schema()
    try:
        set_default_guidance_template(template_id)
        flash("デフォルトの案内テンプレートを更新しました。", "success")
    except ReceivedInvoiceError as exc:
        flash(str(exc), "warning")
    return redirect(url_for("invoice.received_guidance_template_list"))


@invoice_bp.get("/received/<int:request_id>")
@_admin_required
def received_invoice_detail(request_id: int):
    ensure_received_invoice_schema()
    row = get_received_request(request_id=request_id)
    if not row:
        abort(404)
    return render_template("received_invoice_detail.html", invoice=row, statuses=RECEIVED_STATUSES)


@invoice_bp.post("/received/<int:request_id>/send")
@_admin_required
def received_invoice_send(request_id: int):
    ensure_received_invoice_schema()
    row = get_received_request(request_id=request_id)
    if not row:
        abort(404)
    try:
        edit_url = url_for("invoice.received_invoice_public", public_id=row["public_id"], _external=True, _scheme="https")
        send_request_invitation(row, edit_url, request.remote_addr or "")
        flash("相手方へ請求書作成依頼を送信しました。", "success")
    except Exception as exc:
        flash(f"依頼メールを送信できませんでした: {exc}", "danger")
    return redirect(url_for("invoice.received_invoice_detail", request_id=request_id))


@invoice_bp.post("/received/<int:request_id>/action")
@_admin_required
def received_invoice_action(request_id: int):
    ensure_received_invoice_schema()
    row = get_received_request(request_id=request_id)
    if not row:
        abort(404)
    try:
        edit_url = url_for("invoice.received_invoice_public", public_id=row["public_id"], _external=True, _scheme="https")
        admin_action(row, str(request.form.get("action") or ""), str(request.form.get("message") or ""), request.remote_addr or "", edit_url)
        flash("状態を更新しました。", "success")
    except ReceivedInvoiceError as exc:
        flash(str(exc), "warning")
    except Exception as exc:
        flash(f"状態更新後の通知に失敗しました: {exc}", "danger")
    return redirect(url_for("invoice.received_invoice_detail", request_id=request_id))


@invoice_bp.get("/received/<int:request_id>/pdf")
@_admin_required
def received_invoice_pdf(request_id: int):
    ensure_received_invoice_schema()
    row = get_received_request(request_id=request_id)
    if not row:
        abort(404)
    path = str(row.get("pdf_storage_path") or "")
    if not path or not os.path.isfile(path):
        try:
            path, _, _ = generate_received_pdf(row)
        except Exception as exc:
            flash(f"PDFを生成できませんでした: {exc}", "danger")
            return redirect(url_for("invoice.received_invoice_detail", request_id=request_id))
    return send_file(path, as_attachment=False, download_name=os.path.basename(path), mimetype="application/pdf")


@invoice_bp.get("/received/<int:request_id>/attachment/<int:attachment_id>")
@_admin_required
def received_invoice_attachment(request_id: int, attachment_id: int):
    row = get_received_request(request_id=request_id)
    if not row:
        abort(404)
    attachment = next((item for item in row["attachments"] if int(item["id"]) == attachment_id), None)
    if not attachment or not os.path.isfile(attachment["storage_path"]):
        abort(404)
    return send_file(attachment["storage_path"], as_attachment=True, download_name=attachment["original_name"], mimetype=attachment.get("content_type") or "application/octet-stream")


@invoice_bp.get("/received/<int:request_id>/version/<int:version_id>/pdf")
@_admin_required
def received_invoice_version_pdf(request_id: int, version_id: int):
    row = get_received_request(request_id=request_id)
    if not row:
        abort(404)
    version = next((item for item in row["versions"] if int(item["id"]) == version_id), None)
    if not version or not os.path.isfile(version["pdf_storage_path"]):
        abort(404)
    return send_file(version["pdf_storage_path"], as_attachment=False, mimetype="application/pdf")


@invoice_bp.get("/request/<public_id>")
def received_invoice_public(public_id: str):
    row = _load_public(public_id)
    g.mfu_access_log_marker = "[RECEIVED_INVOICE_VIEW]"
    return render_template(
        "received_invoice_public.html",
        invoice=row,
        authorized=_is_granted(row),
        masked_email=mask_email(row["counterparty_email"]),
        statuses=RECEIVED_STATUSES,
    )


@invoice_bp.post("/request/<public_id>/otp/send")
def received_invoice_otp_send(public_id: str):
    row = _load_public(public_id)
    try:
        send_received_otp(
            row,
            request.remote_addr or "",
            url_for("invoice.received_invoice_public", public_id=public_id, _external=True, _scheme="https"),
        )
        flash("認証コードをメールで送信しました。", "success")
    except ReceivedInvoiceError as exc:
        flash(str(exc), "warning")
    except Exception as exc:
        flash(f"認証コードを送信できませんでした: {exc}", "danger")
    return redirect(url_for("invoice.received_invoice_public", public_id=public_id))


@invoice_bp.post("/request/<public_id>/otp/verify")
def received_invoice_otp_verify(public_id: str):
    row = _load_public(public_id)
    if verify_received_otp(row, str(request.form.get("code") or ""), request.remote_addr or ""):
        session[_grant_key(public_id)] = {"version": int(row["auth_version"]), "until": time.time() + GRANT_SECONDS}
        flash("メール認証が完了しました。", "success")
    else:
        flash("認証コードが正しくないか、有効期限が切れています。", "warning")
    return redirect(url_for("invoice.received_invoice_public", public_id=public_id))


@invoice_bp.post("/request/<public_id>/save")
def received_invoice_public_save(public_id: str):
    row = _load_public(public_id)
    try:
        _require_grant(row)
        action = str(request.form.get("action") or "save")
        submit = action == "submit"
        attachments = request.files.getlist("attachments")
        updated = update_recipient_draft(
            row, request.form, attachments, request.remote_addr or "", submit=submit,
        )
        if action == "preview":
            pdf_data = render_received_pdf_bytes(updated)
            response = send_file(
                BytesIO(pdf_data),
                mimetype="application/pdf",
                as_attachment=False,
                download_name="請求書プレビュー.pdf",
            )
            response.headers["X-MFU-Content-Version"] = str(updated.get("content_version") or "")
            response.headers["X-MFU-Saved-Attachments"] = str(len([item for item in attachments if item and item.filename]))
            return response
        if submit:
            path, filename, pdf_data = generate_received_pdf(updated)
            updated["pdf_storage_path"] = path
            notification_failed = False
            try:
                notify_submitted(updated, filename, pdf_data)
                record_event(updated["id"], "submission_notifications_sent", "system", request.remote_addr or "")
            except Exception as exc:
                notification_failed = True
                record_event(updated["id"], "submission_notification_failed", "system", request.remote_addr or "", {"error": str(exc)[:500]})
                current_app.logger.exception("received invoice submission mail failed")
            session.pop(_grant_key(public_id), None)
            if notification_failed:
                flash("請求書は提出できましたが、控えメールの送信に失敗しました。管理者へご連絡ください。", "warning")
            else:
                flash("請求書を提出しました。控えをメールでお送りします。", "success")
        else:
            flash("入力内容を一時保存しました。", "success")
    except ReceivedInvoiceError as exc:
        flash(str(exc), "warning")
    except Exception as exc:
        flash(f"保存できませんでした: {exc}", "danger")
    return redirect(url_for("invoice.received_invoice_public", public_id=public_id))
