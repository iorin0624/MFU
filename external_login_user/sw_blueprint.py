from flask import Blueprint, current_app, send_from_directory

sw_bp = Blueprint("sw_root", __name__, url_prefix="")


@sw_bp.get("/sw.js")
def root_sw():
    resp = send_from_directory(current_app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Type"] = "application/javascript"
    return resp
