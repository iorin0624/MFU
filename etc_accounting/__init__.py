from flask import Blueprint


etc_accounting_bp = Blueprint(
    "etc_accounting",
    __name__,
    template_folder="templates",
    url_prefix="/etc-accounting",
)


from . import routes  # noqa: E402,F401
