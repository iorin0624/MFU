from __future__ import annotations

from flask import Blueprint

from .schema import ensure_bank_account_schema

bank_account_bp = Blueprint(
    "bank_account",
    __name__,
    template_folder="templates",
    static_folder="static",
)


def register_bank_account(app) -> None:
    ensure_bank_account_schema()
    app.register_blueprint(bank_account_bp)


from . import routes  # noqa: E402,F401
