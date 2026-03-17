from flask import Blueprint

fare_bp = Blueprint("fare", __name__, template_folder="template")

from . import routes  # noqa: E402,F401
