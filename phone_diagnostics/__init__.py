from flask import Blueprint


phone_diagnostics_bp = Blueprint(
    "phone_diagnostics",
    __name__,
    template_folder="templates",
)


from .routes import (  # noqa: E402,F401
    ensure_phone_diagnostics_nav_item,
    ensure_phone_diagnostics_schema,
)
