from flask import Blueprint

shipment_tracking_bp = Blueprint(
    "shipment_tracking",
    __name__,
    template_folder="template",
)

from . import routes  # noqa: E402,F401
