from flask import Blueprint


recurring_expenses_bp = Blueprint(
    "recurring_expenses",
    __name__,
    template_folder="templates",
    url_prefix="/recurring-expenses",
)


from . import routes  # noqa: E402,F401
