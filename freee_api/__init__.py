from __future__ import annotations

from flask import Blueprint

freee_api_bp = Blueprint(
    "freee_api",
    __name__,
    url_prefix="/freee_api",
    template_folder="templates",
)

from . import routes  # noqa: E402,F401

