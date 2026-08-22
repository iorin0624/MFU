from flask import Blueprint


tdr_bp = Blueprint(
    "tdr",
    __name__,
    template_folder="popcorn/templates",
    url_prefix="/tdr",
)

from .popcorn import routes  # noqa: E402,F401

