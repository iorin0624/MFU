"""INPA application factories.

The public and internal-admin applications deliberately use separate factories.
Only the internal process registers the Internal Admin API blueprint.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flask import Flask, jsonify

from .auth.routes import bp as auth_bp
from .config import apply_settings
from .db import init_app as init_db
from .health import bp as health_bp
from .internal_admin import bp as internal_admin_bp
from .planner import bp as planner_bp
from .public import bp as public_bp
from .relationships import bp as relationships_bp
from .share import bp as share_bp


def create_app(role: str, overrides: Mapping[str, Any] | None = None) -> Flask:
    """Create either the externally reachable or Unix-socket-only application."""
    if role not in {"public", "admin"}:
        raise ValueError("role must be 'public' or 'admin'")

    app = Flask(__name__)
    apply_settings(app, role=role, overrides=overrides)
    init_db(app)
    app.register_blueprint(health_bp)

    if role == "public":
        app.register_blueprint(public_bp)
        app.register_blueprint(auth_bp)
        app.register_blueprint(planner_bp)
        app.register_blueprint(relationships_bp)
        app.register_blueprint(share_bp)
    else:
        app.register_blueprint(internal_admin_bp)

    @app.errorhandler(404)
    def not_found(_: object):
        return jsonify(error={"code": "not_found", "message": "Not found."}), 404

    @app.errorhandler(500)
    def internal_error(_: object):
        return jsonify(error={"code": "internal_error", "message": "An unexpected error occurred."}), 500

    return app
