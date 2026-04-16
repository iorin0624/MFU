from __future__ import annotations

import json

from flask import flash, redirect, render_template, request, url_for

from . import shipment_tracking_bp
from .models import CARRIER_MASTER
from .services import (
    ShipmentTrackingError,
    create_target,
    get_logs,
    get_target,
    list_targets,
    run_check,
    toggle_target_active,
    update_target,
)

try:
    from app import admin_required  # type: ignore
except Exception:
    from functools import wraps
    from flask import session

    def admin_required(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if session.get("user") != "admin":
                return "管理者のみアクセス可能", 403
            return func(*args, **kwargs)

        return wrapper


@shipment_tracking_bp.route("/admin/shipment-tracking", methods=["GET"])
@admin_required
def shipment_tracking_list():
    targets = list_targets()
    return render_template(
        "admin/shipment_tracking/list.html",
        targets=targets,
        carrier_master=CARRIER_MASTER,
    )


@shipment_tracking_bp.route("/admin/shipment-tracking/new", methods=["GET", "POST"])
@admin_required
def shipment_tracking_new():
    if request.method == "POST":
        try:
            target_id = create_target(
                carrier_code=(request.form.get("carrier_code") or "").strip(),
                tracking_number=request.form.get("tracking_number") or "",
                label=request.form.get("label"),
                is_active=bool(request.form.get("is_active")),
            )
            flash("配送追跡を登録しました", "success")

            ok = run_check(target_id, "initial")
            if ok:
                flash("初回確認を実行しました", "success")
            else:
                flash("初回確認に失敗しました", "danger")

            return redirect(url_for("shipment_tracking.shipment_tracking_detail", id=target_id))
        except ShipmentTrackingError as exc:
            flash(str(exc), "danger")

    return render_template(
        "admin/shipment_tracking/form.html",
        mode="new",
        target={"label": "", "carrier_code": "sagawa", "tracking_number": "", "is_active": 1},
        carrier_master=CARRIER_MASTER,
    )


@shipment_tracking_bp.route("/admin/shipment-tracking/<int:id>", methods=["GET"])
@admin_required
def shipment_tracking_detail(id: int):
    target = get_target(id)
    if not target:
        flash("対象が見つかりません。", "danger")
        return redirect(url_for("shipment_tracking.shipment_tracking_list"))

    logs = get_logs(id, limit=20)
    pretty_payload = None
    if target.get("last_payload_json"):
        try:
            pretty_payload = json.dumps(json.loads(target["last_payload_json"]), ensure_ascii=False, indent=2)
        except Exception:
            pretty_payload = target["last_payload_json"]

    return render_template(
        "admin/shipment_tracking/detail.html",
        target=target,
        logs=logs,
        pretty_payload=pretty_payload,
        carrier_master=CARRIER_MASTER,
    )


@shipment_tracking_bp.route("/admin/shipment-tracking/<int:id>/edit", methods=["GET", "POST"])
@admin_required
def shipment_tracking_edit(id: int):
    target = get_target(id)
    if not target:
        flash("対象が見つかりません。", "danger")
        return redirect(url_for("shipment_tracking.shipment_tracking_list"))

    if request.method == "POST":
        try:
            update_target(
                target_id=id,
                carrier_code=(request.form.get("carrier_code") or "").strip(),
                tracking_number=request.form.get("tracking_number") or "",
                label=request.form.get("label"),
                is_active=bool(request.form.get("is_active")),
            )
            flash("配送追跡を更新しました", "success")
            return redirect(url_for("shipment_tracking.shipment_tracking_detail", id=id))
        except ShipmentTrackingError as exc:
            flash(str(exc), "danger")

        target = {
            **target,
            "label": request.form.get("label", ""),
            "carrier_code": request.form.get("carrier_code", target["carrier_code"]),
            "tracking_number": request.form.get("tracking_number", target["tracking_number"]),
            "is_active": 1 if request.form.get("is_active") else 0,
        }

    return render_template(
        "admin/shipment_tracking/form.html",
        mode="edit",
        target=target,
        carrier_master=CARRIER_MASTER,
    )


@shipment_tracking_bp.route("/admin/shipment-tracking/<int:id>/check-now", methods=["POST"])
@admin_required
def shipment_tracking_check_now(id: int):
    target = get_target(id)
    if not target:
        flash("対象が見つかりません。", "danger")
        return redirect(url_for("shipment_tracking.shipment_tracking_list"))

    ok = run_check(id, "manual")
    if ok:
        flash("最新情報を確認しました", "success")
    else:
        flash("最新情報の確認に失敗しました", "danger")
    return redirect(url_for("shipment_tracking.shipment_tracking_detail", id=id))


@shipment_tracking_bp.route("/admin/shipment-tracking/<int:id>/toggle-active", methods=["POST"])
@admin_required
def shipment_tracking_toggle_active(id: int):
    try:
        is_active = toggle_target_active(id)
        flash("有効にしました" if is_active else "無効にしました", "success")
    except ShipmentTrackingError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("shipment_tracking.shipment_tracking_detail", id=id))
