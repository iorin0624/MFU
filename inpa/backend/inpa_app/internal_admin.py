"""Internal-only routes. HMAC authentication is added before MFU bridge integration."""

from __future__ import annotations

from flask import Blueprint, jsonify

bp = Blueprint("internal_admin", __name__, url_prefix="/internal/admin/v1")


@bp.get("/bootstrap-status")
def bootstrap_status():
    """Socket-only smoke-test endpoint; it exposes no INPA user data."""
    return jsonify(service="inpa", status="internal_api_bootstrap")
