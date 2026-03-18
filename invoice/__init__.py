from __future__ import annotations

from flask import Blueprint

invoice_bp = Blueprint(
    "invoice",
    __name__,
    url_prefix="/invoice",
    template_folder="template",
)

from . import routes  # noqa: E402,F401
