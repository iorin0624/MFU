from flask import Blueprint

receipts_bp = Blueprint(
    "receipts",
    __name__,
    template_folder="template",
)

from . import routes  # noqa: E402
