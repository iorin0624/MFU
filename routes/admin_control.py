from flask import Blueprint, request, abort
from flask_login import login_required, current_user
import os
from app.utils.admin_passkey_stepup import require_admin_passkey

admin_control_bp = Blueprint("admin_control", __name__)

@admin_control_bp.route("/admin/reboot", methods=["POST"])
@login_required
def reboot():
    if current_user.username != "admin":
        abort(403)
    guard = require_admin_passkey("server_reboot")
    if guard:
        return guard
    os.system("sudo reboot")
    return "再起動を実行しました"

@admin_control_bp.route("/admin/shutdown", methods=["POST"])
@login_required
def shutdown():
    if current_user.username != "admin":
        abort(403)
    guard = require_admin_passkey("server_shutdown")
    if guard:
        return guard
    os.system("sudo shutdown -h now")
    return "シャットダウンを実行しました"

from flask import render_template

@admin_control_bp.route("/admin/control", methods=["GET"])
@login_required
def control_panel():
    if current_user.username != "admin":
        abort(403)
    return render_template("admin_controls.html")
