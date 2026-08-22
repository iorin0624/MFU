from flask import Blueprint


signage_bp = Blueprint(
    "signage",
    __name__,
    template_folder="templates",
    url_prefix="/signage",
)

signage_admin_bp = Blueprint(
    "signage_admin",
    __name__,
    template_folder="templates",
    url_prefix="/admin/signage",
)

train_status_bp = Blueprint(
    "train_status",
    __name__,
    template_folder="templates",
)


from . import routes  # noqa: E402,F401
from . import train_status  # noqa: E402,F401
