from __future__ import annotations

from flask import Blueprint

image_viewer_bp = Blueprint(
    "image_viewer",
    __name__,
    template_folder="template",
    url_prefix="/image_viewer",
)

from . import routes  # noqa: E402,F401
