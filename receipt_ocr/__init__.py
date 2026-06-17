from flask import Blueprint

receipt_ocr_bp = Blueprint(
    "receipt_ocr",
    __name__,
    template_folder="template",
    url_prefix="/receipt_ocr",
)

from . import routes  # noqa: E402
