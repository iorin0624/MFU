from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app.utils.db import get_db

from .models import ensure_records_schema, now_ts

records_bp = Blueprint(
    "records",
    __name__,
    template_folder="templates",
    static_folder="static",
)


@records_bp.before_app_first_request
def _init_records_schema() -> None:
    ensure_records_schema()


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            flash("ログインが必要です。", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


def _parse_date(value: str, field_name: str) -> date | None:
    if not value:
        flash(f"{field_name}を入力してください。", "warning")
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        flash(f"{field_name}の形式が正しくありません。", "warning")
        return None


def _parse_int(value: str, field_name: str, *, allow_empty: bool = False) -> int | None:
    if value is None or value == "":
        if allow_empty:
            return None
        flash(f"{field_name}を入力してください。", "warning")
        return None
    try:
        return int(value)
    except ValueError:
        flash(f"{field_name}は数値で入力してください。", "warning")
        return None


def _parse_decimal(value: str, field_name: str) -> Decimal | None:
    if value is None or value == "":
        flash(f"{field_name}を入力してください。", "warning")
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        flash(f"{field_name}は数値で入力してください。", "warning")
        return None


@records_bp.get("/")
@login_required
def index():
    return render_template("index.html")


@records_bp.get("/uber")
@login_required
def uber_list():
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT
            id,
            work_date,
            deliveries,
            net_yen,
            promo_yen,
            other_yen,
            tip_yen,
            (net_yen + promo_yen + other_yen + tip_yen) AS total_yen
        FROM uber_daily
        ORDER BY work_date DESC
        """
    )
    rows = cur.fetchall()

    today = date.today()
    month_start = date(today.year, today.month, 1)
    if today.month == 12:
        month_end = date(today.year + 1, 1, 1)
    else:
        month_end = date(today.year, today.month + 1, 1)

    cur.execute(
        """
        SELECT
            COALESCE(SUM(deliveries), 0) AS deliveries_sum,
            COALESCE(SUM(net_yen), 0) AS net_sum,
            COALESCE(SUM(net_yen + promo_yen + other_yen + tip_yen), 0) AS total_sum
        FROM uber_daily
        WHERE work_date >= %s AND work_date < %s
        """,
        (month_start, month_end),
    )
    summary = cur.fetchone() or {}
    db.close()

    for row in rows:
        deliveries = row.get("deliveries") or 0
        total_yen = row.get("total_yen") or 0
        row["avg_yen"] = round(total_yen / deliveries) if deliveries else None

    deliveries_sum = summary.get("deliveries_sum") or 0
    total_sum = summary.get("total_sum") or 0
    summary_avg = round(total_sum / deliveries_sum) if deliveries_sum else None

    return render_template(
        "uber/list.html",
        rows=rows,
        summary={
            "deliveries_sum": deliveries_sum,
            "net_sum": summary.get("net_sum") or 0,
            "total_sum": total_sum,
            "avg_yen": summary_avg,
            "month_start": month_start,
        },
    )


@records_bp.get("/uber/new")
@login_required
def uber_new():
    return render_template("uber/form.html", item=None)


@records_bp.post("/uber/new")
@login_required
def uber_create():
    work_date = _parse_date(request.form.get("work_date", ""), "日付")
    deliveries = _parse_int(request.form.get("deliveries", ""), "件数")
    net_yen = _parse_int(request.form.get("net_yen", ""), "正味の料金")
    promo_yen = _parse_int(request.form.get("promo_yen", "0"), "プロモーション")
    other_yen = _parse_int(request.form.get("other_yen", "0"), "その他")
    tip_yen = _parse_int(request.form.get("tip_yen", "0"), "チップ")
    if None in (work_date, deliveries, net_yen, promo_yen, other_yen, tip_yen):
        return redirect(url_for("records.uber_new"))

    now = now_ts()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO uber_daily (
            work_date,
            deliveries,
            net_yen,
            promo_yen,
            other_yen,
            tip_yen,
            created_at,
            updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            deliveries = VALUES(deliveries),
            net_yen = VALUES(net_yen),
            promo_yen = VALUES(promo_yen),
            other_yen = VALUES(other_yen),
            tip_yen = VALUES(tip_yen),
            updated_at = VALUES(updated_at)
        """,
        (
            work_date,
            deliveries,
            net_yen,
            promo_yen,
            other_yen,
            tip_yen,
            now,
            now,
        ),
    )
    db.commit()
    db.close()
    flash("Uber記録を保存しました。", "success")
    return redirect(url_for("records.uber_list"))


@records_bp.get("/uber/<int:record_id>/edit")
@login_required
def uber_edit(record_id: int):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM uber_daily WHERE id = %s", (record_id,))
    item = cur.fetchone()
    db.close()
    if not item:
        flash("対象の記録が見つかりません。", "warning")
        return redirect(url_for("records.uber_list"))
    return render_template("uber/form.html", item=item)


@records_bp.post("/uber/<int:record_id>/edit")
@login_required
def uber_update(record_id: int):
    work_date = _parse_date(request.form.get("work_date", ""), "日付")
    deliveries = _parse_int(request.form.get("deliveries", ""), "件数")
    net_yen = _parse_int(request.form.get("net_yen", ""), "正味の料金")
    promo_yen = _parse_int(request.form.get("promo_yen", "0"), "プロモーション")
    other_yen = _parse_int(request.form.get("other_yen", "0"), "その他")
    tip_yen = _parse_int(request.form.get("tip_yen", "0"), "チップ")
    if None in (work_date, deliveries, net_yen, promo_yen, other_yen, tip_yen):
        return redirect(url_for("records.uber_edit", record_id=record_id))

    now = now_ts()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        UPDATE uber_daily
        SET work_date = %s,
            deliveries = %s,
            net_yen = %s,
            promo_yen = %s,
            other_yen = %s,
            tip_yen = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (
            work_date,
            deliveries,
            net_yen,
            promo_yen,
            other_yen,
            tip_yen,
            now,
            record_id,
        ),
    )
    db.commit()
    db.close()
    flash("Uber記録を更新しました。", "success")
    return redirect(url_for("records.uber_list"))


@records_bp.post("/uber/<int:record_id>/delete")
@login_required
def uber_delete(record_id: int):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM uber_daily WHERE id = %s", (record_id,))
    db.commit()
    db.close()
    flash("Uber記録を削除しました。", "success")
    return redirect(url_for("records.uber_list"))


@records_bp.get("/maintenance")
@login_required
def maintenance_list():
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT *
        FROM bike_maintenance_log
        ORDER BY event_date DESC, odometer_km DESC, id DESC
        """
    )
    rows = cur.fetchall()

    cur.execute(
        """
        SELECT m.*
        FROM bike_maintenance_log m
        WHERE m.id = (
            SELECT m2.id
            FROM bike_maintenance_log m2
            WHERE m2.item = m.item
            ORDER BY m2.event_date DESC, m2.odometer_km DESC, m2.id DESC
            LIMIT 1
        )
        ORDER BY m.item
        """
    )
    latest_rows = cur.fetchall()
    db.close()

    return render_template(
        "maintenance/list.html",
        rows=rows,
        latest_rows=latest_rows,
    )


@records_bp.get("/maintenance/new")
@login_required
def maintenance_new():
    return render_template("maintenance/form.html", item=None)


@records_bp.post("/maintenance/new")
@login_required
def maintenance_create():
    event_date = _parse_date(request.form.get("event_date", ""), "日付")
    odometer_km = _parse_int(request.form.get("odometer_km", ""), "メーター")
    item = request.form.get("item", "").strip()
    note = request.form.get("note", "").strip() or None
    if not item:
        flash("項目を入力してください。", "warning")
    if event_date is None or odometer_km is None or not item:
        return redirect(url_for("records.maintenance_new"))

    now = now_ts()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO bike_maintenance_log (
            event_date,
            odometer_km,
            item,
            note,
            created_at,
            updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (event_date, odometer_km, item, note, now, now),
    )
    db.commit()
    db.close()
    flash("整備記録を追加しました。", "success")
    return redirect(url_for("records.maintenance_list"))


@records_bp.get("/maintenance/<int:record_id>/edit")
@login_required
def maintenance_edit(record_id: int):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM bike_maintenance_log WHERE id = %s", (record_id,))
    item = cur.fetchone()
    db.close()
    if not item:
        flash("対象の記録が見つかりません。", "warning")
        return redirect(url_for("records.maintenance_list"))
    return render_template("maintenance/form.html", item=item)


@records_bp.post("/maintenance/<int:record_id>/edit")
@login_required
def maintenance_update(record_id: int):
    event_date = _parse_date(request.form.get("event_date", ""), "日付")
    odometer_km = _parse_int(request.form.get("odometer_km", ""), "メーター")
    item = request.form.get("item", "").strip()
    note = request.form.get("note", "").strip() or None
    if not item:
        flash("項目を入力してください。", "warning")
    if event_date is None or odometer_km is None or not item:
        return redirect(url_for("records.maintenance_edit", record_id=record_id))

    now = now_ts()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        UPDATE bike_maintenance_log
        SET event_date = %s,
            odometer_km = %s,
            item = %s,
            note = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (event_date, odometer_km, item, note, now, record_id),
    )
    db.commit()
    db.close()
    flash("整備記録を更新しました。", "success")
    return redirect(url_for("records.maintenance_list"))


@records_bp.post("/maintenance/<int:record_id>/delete")
@login_required
def maintenance_delete(record_id: int):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM bike_maintenance_log WHERE id = %s", (record_id,))
    db.commit()
    db.close()
    flash("整備記録を削除しました。", "success")
    return redirect(url_for("records.maintenance_list"))


@records_bp.get("/fuel")
@login_required
def fuel_list():
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT *
        FROM bike_fuel_log
        ORDER BY odometer_km ASC, fill_date ASC, id ASC
        """
    )
    rows = cur.fetchall()
    db.close()

    computed = []
    prev = None
    for row in rows:
        distance = None
        km_per_l = None
        yen_per_km = None
        if prev is not None:
            distance_val = row["odometer_km"] - prev["odometer_km"]
            if distance_val > 0:
                distance = distance_val
                liters = float(row["liters"])
                if liters > 0:
                    km_per_l = distance / liters
                if row.get("yen_per_liter"):
                    yen_per_km = (row["yen_per_liter"] * liters) / distance
        row["distance_km"] = distance
        row["km_per_l"] = km_per_l
        row["yen_per_km"] = yen_per_km
        computed.append(row)
        prev = row

    computed.sort(
        key=lambda r: (
            r["fill_date"],
            r["odometer_km"],
            r["id"],
        ),
        reverse=True,
    )

    return render_template("fuel/list.html", rows=computed)


@records_bp.get("/fuel/new")
@login_required
def fuel_new():
    return render_template("fuel/form.html", item=None)


@records_bp.post("/fuel/new")
@login_required
def fuel_create():
    fill_date = _parse_date(request.form.get("fill_date", ""), "日付")
    odometer_km = _parse_int(request.form.get("odometer_km", ""), "メーター")
    liters = _parse_decimal(request.form.get("liters", ""), "給油量")
    yen_per_liter = _parse_int(
        request.form.get("yen_per_liter", ""),
        "円/L",
        allow_empty=True,
    )
    note = request.form.get("note", "").strip() or None
    is_full = 1 if request.form.get("is_full") == "on" else 0
    if fill_date is None or odometer_km is None or liters is None:
        return redirect(url_for("records.fuel_new"))

    now = now_ts()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO bike_fuel_log (
            fill_date,
            odometer_km,
            liters,
            yen_per_liter,
            is_full,
            note,
            created_at,
            updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (fill_date, odometer_km, liters, yen_per_liter, is_full, note, now, now),
    )
    db.commit()
    db.close()
    flash("給油記録を追加しました。", "success")
    return redirect(url_for("records.fuel_list"))


@records_bp.get("/fuel/<int:record_id>/edit")
@login_required
def fuel_edit(record_id: int):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM bike_fuel_log WHERE id = %s", (record_id,))
    item = cur.fetchone()
    db.close()
    if not item:
        flash("対象の記録が見つかりません。", "warning")
        return redirect(url_for("records.fuel_list"))
    return render_template("fuel/form.html", item=item)


@records_bp.post("/fuel/<int:record_id>/edit")
@login_required
def fuel_update(record_id: int):
    fill_date = _parse_date(request.form.get("fill_date", ""), "日付")
    odometer_km = _parse_int(request.form.get("odometer_km", ""), "メーター")
    liters = _parse_decimal(request.form.get("liters", ""), "給油量")
    yen_per_liter = _parse_int(
        request.form.get("yen_per_liter", ""),
        "円/L",
        allow_empty=True,
    )
    note = request.form.get("note", "").strip() or None
    is_full = 1 if request.form.get("is_full") == "on" else 0
    if fill_date is None or odometer_km is None or liters is None:
        return redirect(url_for("records.fuel_edit", record_id=record_id))

    now = now_ts()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        UPDATE bike_fuel_log
        SET fill_date = %s,
            odometer_km = %s,
            liters = %s,
            yen_per_liter = %s,
            is_full = %s,
            note = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (fill_date, odometer_km, liters, yen_per_liter, is_full, note, now, record_id),
    )
    db.commit()
    db.close()
    flash("給油記録を更新しました。", "success")
    return redirect(url_for("records.fuel_list"))


@records_bp.post("/fuel/<int:record_id>/delete")
@login_required
def fuel_delete(record_id: int):
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM bike_fuel_log WHERE id = %s", (record_id,))
    db.commit()
    db.close()
    flash("給油記録を削除しました。", "success")
    return redirect(url_for("records.fuel_list"))
