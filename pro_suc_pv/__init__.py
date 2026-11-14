"""Public profile and calendar page-view analytics."""

from flask import Blueprint

bp = Blueprint("pro_suc_pv", __name__, template_folder="templates")

from . import routes  # noqa: E402,F401
