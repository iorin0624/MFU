from __future__ import annotations

from datetime import date

from flask import current_app, render_template, request

from . import fare_bp
from .services import (
    DEFAULT_FROM_PLACE,
    FareEstimateError,
    build_yahoo_transit_url,
    calculate_total_fare,
    fetch_transit_html,
    parse_route1_fare,
    validate_destination,
    validate_parking_fee,
    validate_target_date,
)


DEFAULT_ERROR_MESSAGE = "現在、交通費概算を取得できません。時間をおいてお試しください。"
INPUT_ERROR_MESSAGE = "概算交通費を取得できませんでした。到着地点または日付をご確認ください。"


@fare_bp.route("/fare-estimate", methods=["GET", "POST"])
def fare_estimate():
    today = date.today()
    form_data = {
        "destination": "",
        "target_date": today.isoformat(),
        "parking_fee": "",
    }
    result = None
    error_message = None

    if request.method == "POST":
        form_data["destination"] = request.form.get("destination", "")
        form_data["target_date"] = request.form.get("target_date", today.isoformat())
        form_data["parking_fee"] = request.form.get("parking_fee", "")

        try:
            destination = validate_destination(form_data["destination"])
            target_date = validate_target_date(form_data["target_date"])
            parking_fee = validate_parking_fee(form_data["parking_fee"])

            yahoo_url = build_yahoo_transit_url(DEFAULT_FROM_PLACE, destination, target_date)
            html = fetch_transit_html(yahoo_url)
            one_way_fare = parse_route1_fare(html)
            fares = calculate_total_fare(one_way_fare, parking_fee)

            result = {
                "from_place": DEFAULT_FROM_PLACE,
                "destination": destination,
                "target_date": target_date.isoformat(),
                **fares,
            }
        except FareEstimateError as exc:
            message = str(exc)
            if message in {
                "到着地点を入力してください。",
                "利用日を正しく入力してください。",
                "駐輪場代は0以上の数値で入力してください。",
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
        except Exception as exc:
            current_app.logger.exception(
                "[fare_estimate] fetch failed destination=%s date=%s",
                form_data["destination"],
                form_data["target_date"],
            )
            error_message = DEFAULT_ERROR_MESSAGE

    return render_template(
        "fare_estimate.html",
        form_data=form_data,
        result=result,
        error_message=error_message,
    )
