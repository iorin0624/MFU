from __future__ import annotations

from datetime import date
from functools import wraps

from flask import current_app, flash, g, redirect, render_template, request, session, url_for

from . import fare_bp
from .audit import build_fare_search_marker
from .services import (
    FareEstimateError,
    build_yahoo_transit_url,
    calculate_total_fare,
    create_default_fare_estimate_setting_if_missing,
    fetch_transit_html,
    get_fare_estimate_setting,
    parse_route1_fare,
    update_fare_estimate_setting,
    validate_destination,
    validate_from_place,
    validate_parking_fee,
    validate_target_date,
)


DEFAULT_ERROR_MESSAGE = "現在、交通費概算を取得できません。時間をおいてお試しください。"
INPUT_ERROR_MESSAGE = "概算交通費を取得できませんでした。到着地点または日付をご確認ください。"


try:
    from app import admin_required  # type: ignore
except Exception:
    def admin_required(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if session.get("user") != "admin":
                return "管理者のみアクセス可能", 403
            return func(*args, **kwargs)

        return wrapper


@fare_bp.route("/fare-estimate", methods=["GET", "POST"])
def fare_estimate():
    today = date.today()
    form_data = {
        "destination": "",
        "target_date": today.isoformat(),
    }
    result = None
    error_message = None

    try:
        setting = get_fare_estimate_setting()
    except Exception:
        current_app.logger.exception("[fare_estimate] setting fetch failed")
        setting = {"from_place": "五井", "parking_fee": 100}

    if request.method == "POST":
        form_data["destination"] = request.form.get("destination", "")
        form_data["target_date"] = request.form.get("target_date", today.isoformat())
        search_succeeded = False

        try:
            destination = validate_destination(form_data["destination"])
            target_date = validate_target_date(form_data["target_date"])

            yahoo_url = build_yahoo_transit_url(str(setting["from_place"]), destination, target_date)
            html = fetch_transit_html(yahoo_url)
            one_way_fare = parse_route1_fare(html)
            fares = calculate_total_fare(one_way_fare, int(setting["parking_fee"]))

            result = {
                "destination": destination,
                "target_date": target_date.isoformat(),
                **fares,
            }
            search_succeeded = True
        except FareEstimateError as exc:
            message = str(exc)
            if message in {
                "到着地点を入力してください。",
                "利用日を正しく入力してください。",
            } or message.endswith("文字以内で入力してください。"):
                error_message = message
            else:
                current_app.logger.warning(
                    "[fare_estimate] fare parsing failed destination=%s date=%s reason=%s",
                    form_data["destination"],
                    form_data["target_date"],
                    message,
                )
                error_message = INPUT_ERROR_MESSAGE
        except Exception:
            current_app.logger.exception(
                "[fare_estimate] fetch failed destination=%s date=%s",
                form_data["destination"],
                form_data["target_date"],
            )
            error_message = DEFAULT_ERROR_MESSAGE
        finally:
            # The global after_request hook stores this marker in the same
            # access-log row as the executed POST. GET requests never reach
            # this branch.
            g.mfu_access_log_marker = build_fare_search_marker(
                form_data["destination"],
                succeeded=search_succeeded,
            )

    return render_template(
        "fare_estimate.html",
        form_data=form_data,
        result=result,
        error_message=error_message,
    )


@fare_bp.route("/admin/fare-estimate", methods=["GET", "POST"])
@admin_required
def admin_fare_estimate():
    create_default_fare_estimate_setting_if_missing()
    setting = get_fare_estimate_setting()

    form_data = {
        "from_place": str(setting["from_place"]),
        "parking_fee": str(setting["parking_fee"]),
    }

    if request.method == "POST":
        form_data["from_place"] = request.form.get("from_place", "")
        form_data["parking_fee"] = request.form.get("parking_fee", "")

        try:
            from_place = validate_from_place(form_data["from_place"])
            parking_fee = validate_parking_fee(form_data["parking_fee"])
            update_fare_estimate_setting(from_place, parking_fee)
            flash("交通費概算設定を更新しました。", "success")
            return redirect(url_for("fare.admin_fare_estimate"))
        except FareEstimateError as exc:
            flash(str(exc), "danger")
        except Exception:
            current_app.logger.exception("[fare_estimate_admin] setting update failed")
            flash("設定の更新に失敗しました。", "danger")

    return render_template("admin_fare_estimate.html", form_data=form_data)
