from flask import Blueprint


phone_whitelist_bp = Blueprint(
    "phone_whitelist",
    __name__,
    template_folder="templates",
)


from .routes import (  # noqa: E402,F401
    ensure_phone_whitelist_nav_item,
    ensure_phone_whitelist_schema,
)

