"""Public routes and the minimal same-origin API surface."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, redirect, send_from_directory

bp = Blueprint("public", __name__)


@bp.get("/api/v1/bootstrap")
def bootstrap():
    """Unauthenticated capability endpoint for the Vue bootstrap flow."""
    return jsonify(service="inpa", api_version="v1", authentication="cookie_session")


@bp.get("/<token>")
def short_share(token: str):
    if len(token) == 20 and all(char in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for char in token):
        return redirect(f"/share/{token}", code=302)
    return frontend(token)


@bp.get("/")
@bp.get("/<path:path>")
def frontend(path: str = ""):
    # API namespaces must never fall through to the SPA history fallback.
    if path.startswith(("api/", "internal/", "health/")):
        abort(404)
    dist_dir = Path(current_app.config["FRONTEND_DIST"])
    requested = dist_dir / path
    if path and requested.is_file():
        return send_from_directory(dist_dir, path)
    index = dist_dir / "index.html"
    if index.is_file():
        return send_from_directory(dist_dir, "index.html")
    return jsonify(service="inpa", status="frontend_not_built"), 503
