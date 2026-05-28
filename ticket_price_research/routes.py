from flask import jsonify, render_template

from app import admin_required

from . import ticket_price_research_bp
from .services import fetch_disney_ticket_items


@ticket_price_research_bp.route("/disney", methods=["GET"])
@admin_required
def disney():
    return render_template("ticket_price_research/disney.html")


@ticket_price_research_bp.route("/disney/fetch", methods=["POST"])
@admin_required
def disney_fetch():
    return jsonify(fetch_disney_ticket_items())
