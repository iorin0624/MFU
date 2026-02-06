from flask import Blueprint

from app.utils.feature_access import enforce_feature_access

receipts_bp = Blueprint(
    "receipts",
    __name__,
    template_folder="template",
)


@receipts_bp.before_request
def _receipts_feature_guard():
    response = enforce_feature_access("events")
    if response is not None:
        return response

from . import routes  # noqa: E402
