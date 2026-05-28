from flask import Blueprint


ticket_price_research_bp = Blueprint(
    "ticket_price_research",
    __name__,
    template_folder="templates",
    url_prefix="/admin/ticket-price",
)

from . import routes  # noqa: E402,F401
