from datetime import datetime
from urllib.parse import quote

from flask import Response, jsonify, render_template, request

from app import admin_required

from . import ticket_price_research_bp
from .pdf import TicketPricePdfError, render_disney_ticket_pdf
from .repository import (
    add_recipient,
    delete_recipient,
    list_recipients,
    set_recipient_active,
)
from .services import fetch_disney_ticket_items


@ticket_price_research_bp.route("/disney", methods=["GET"])
@admin_required
def disney():
    return render_template(
        "ticket_price_research/disney.html",
        mail_recipients=list_recipients(),
        request_address="dt@mail.iori0624.jp",
    )


@ticket_price_research_bp.route("/disney/fetch", methods=["POST"])
@admin_required
def disney_fetch():
    return jsonify(fetch_disney_ticket_items())


@ticket_price_research_bp.route("/disney/pdf", methods=["GET"])
@admin_required
def disney_pdf():
    payload = fetch_disney_ticket_items()
    try:
        pdf_bytes = render_disney_ticket_pdf(payload)
    except TicketPricePdfError as exc:
        return str(exc), 503
    filename = f"disney_ticket_prices_{datetime.now():%Y%m%d_%H%M%S}.pdf"
    disposition = "attachment" if request.args.get("download") == "1" else "inline"
    encoded_filename = quote(filename)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={
            "Content-Disposition": (
                f"{disposition}; filename=\"disney_ticket_prices.pdf\"; "
                f"filename*=UTF-8''{encoded_filename}"
            ),
            "Cache-Control": "private, no-store",
        },
    )


@ticket_price_research_bp.route("/disney/mail-recipients", methods=["POST"])
@admin_required
def disney_mail_recipient_add():
    try:
        recipient = add_recipient(request.form.get("email") or "")
        return jsonify({"ok": True, "recipient": recipient})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@ticket_price_research_bp.route(
    "/disney/mail-recipients/<int:recipient_id>/active",
    methods=["POST"],
)
@admin_required
def disney_mail_recipient_active(recipient_id: int):
    payload = request.get_json(silent=True) or {}
    if not set_recipient_active(recipient_id, bool(payload.get("is_active"))):
        return jsonify({"ok": False, "error": "対象が見つかりません"}), 404
    return jsonify({"ok": True})


@ticket_price_research_bp.route(
    "/disney/mail-recipients/<int:recipient_id>",
    methods=["DELETE"],
)
@admin_required
def disney_mail_recipient_delete(recipient_id: int):
    if not delete_recipient(recipient_id):
        return jsonify({"ok": False, "error": "対象が見つかりません"}), 404
    return jsonify({"ok": True})
