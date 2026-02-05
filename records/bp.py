from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from threading import Lock
from zoneinfo import ZoneInfo

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from app.utils.db import get_db

from .models import (
    ensure_records_schema,
    insert_maintenance_item,
    list_maintenance_items,
    now_ts,
    update_maintenance_item,
)

records_bp = Blueprint(
    "records",
    __name__,
    template_folder="templates",
    static_folder="static",
)


_schema_init_lock = Lock()
_schema_initialized = False


@records_bp.before_app_request
def _init_records_schema() -> None:
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_init_lock:
        if _schema_initialized:
            return
        ensure_records_schema()
        _schema_initialized = True


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


def _parse_decimal(
    value: str,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> Decimal | None:
    if value is None or value == "":
        if allow_empty:
            return None
        flash(f"{field_name}を入力してください。", "warning")
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        flash(f"{field_name}は数値で入力してください。", "warning")
        return None


def _round_decimal(value: Decimal | None, places: int) -> Decimal | None:
    if value is None:
        return None
    quantizer = Decimal("1").scaleb(-places)
    return value.quantize(quantizer, rounding=ROUND_HALF_UP)


def _is_admin_user() -> bool:
    return session.get("user") == "admin"


def _require_admin_for_records():
    if not _is_admin_user():
        flash("管理者のみ操作できます。", "warning")
        return redirect(url_for("records.maintenance_list"))
    return None


@records_bp.get("/")
@login_required
def index():
    # Template collision avoidance: always use the records/ namespace.
    return render_template("records/index.html")


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

    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    month_start = date(today.year, today.month, 1)
    if today.month == 12:
        month_end = date(today.year + 1, 1, 1)
    else:
        month_end = date(today.year, today.month + 1, 1)

    cur.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN deliveries > 0 THEN deliveries ELSE 0 END), 0) AS deliveries_sum,
            COALESCE(SUM(CASE WHEN deliveries > 0 THEN net_yen ELSE 0 END), 0) AS net_sum,
            COALESCE(SUM(CASE WHEN deliveries > 0 THEN (net_yen + promo_yen + other_yen + tip_yen) ELSE 0 END), 0) AS total_sum
        FROM uber_daily
        WHERE work_date >= %s AND work_date < %s
        """,
        (month_start, month_end),
    )
    summary = cur.fetchone() or {}

    cur.execute("SELECT MIN(work_date) AS min_date FROM uber_daily")
    min_row = cur.fetchone() or {}
    min_date = min_row.get("min_date")
    if min_date is None:
        min_month_start = date(today.year, 1, 1)
    else:
        min_month_start = date(min_date.year, min_date.month, 1)

    cur.execute(
        """
        WITH RECURSIVE months AS (
            SELECT CAST(%s AS DATE) AS month_start
            UNION ALL
            SELECT DATE_ADD(month_start, INTERVAL 1 MONTH)
            FROM months
            WHERE month_start < %s
        ),
        daily_base AS (
            SELECT
                CAST(DATE_FORMAT(work_date, '%Y-%m-01') AS DATE) AS month_start,
                deliveries,
                net_yen,
                promo_yen,
                other_yen,
                tip_yen,
                ROUND(net_yen / NULLIF(deliveries, 0)) AS net_per_delivery,
                ROUND((net_yen + promo_yen + other_yen + tip_yen) / NULLIF(deliveries, 0)) AS total_per_delivery
            FROM uber_daily
            WHERE work_date >= %s AND work_date < %s
        ),
        monthly_agg AS (
            SELECT
                month_start,
                COALESCE(SUM(CASE WHEN deliveries > 0 THEN 1 ELSE 0 END), 0) AS days_count,
                COALESCE(SUM(CASE WHEN deliveries > 0 THEN deliveries ELSE 0 END), 0) AS deliveries_sum,
                COALESCE(SUM(CASE WHEN deliveries > 0 THEN net_yen ELSE 0 END), 0) AS net_sum,
                COALESCE(SUM(CASE WHEN deliveries > 0 THEN (net_yen + promo_yen + other_yen + tip_yen) ELSE 0 END), 0) AS total_sum
            FROM daily_base
            GROUP BY month_start
        ),
        net_median AS (
            SELECT
                month_start,
                AVG(net_per_delivery) AS net_median
            FROM (
                SELECT
                    month_start,
                    net_per_delivery,
                    ROW_NUMBER() OVER (PARTITION BY month_start ORDER BY net_per_delivery) AS rn,
                    COUNT(*) OVER (PARTITION BY month_start) AS cnt
                FROM daily_base
                WHERE deliveries > 0
            ) ranked
            WHERE rn IN (FLOOR((cnt + 1) / 2), FLOOR((cnt + 2) / 2))
            GROUP BY month_start
        ),
        total_median AS (
            SELECT
                month_start,
                AVG(total_per_delivery) AS total_median
            FROM (
                SELECT
                    month_start,
                    total_per_delivery,
                    ROW_NUMBER() OVER (PARTITION BY month_start ORDER BY total_per_delivery) AS rn,
                    COUNT(*) OVER (PARTITION BY month_start) AS cnt
                FROM daily_base
                WHERE deliveries > 0
            ) ranked
            WHERE rn IN (FLOOR((cnt + 1) / 2), FLOOR((cnt + 2) / 2))
            GROUP BY month_start
        )
        SELECT
            YEAR(months.month_start) AS year,
            MONTH(months.month_start) AS month,
            COALESCE(monthly_agg.days_count, 0) AS days_count,
            COALESCE(monthly_agg.deliveries_sum, 0) AS deliveries_sum,
            COALESCE(monthly_agg.net_sum, 0) AS net_sum,
            COALESCE(monthly_agg.total_sum, 0) AS total_sum,
            CASE
                WHEN COALESCE(monthly_agg.deliveries_sum, 0) = 0 THEN NULL
                ELSE ROUND(monthly_agg.net_sum / monthly_agg.deliveries_sum)
            END AS net_avg,
            net_median.net_median AS net_median,
            CASE
                WHEN COALESCE(monthly_agg.deliveries_sum, 0) = 0 THEN NULL
                ELSE ROUND(monthly_agg.total_sum / monthly_agg.deliveries_sum)
            END AS total_avg,
            total_median.total_median AS total_median
        FROM months
        LEFT JOIN monthly_agg ON monthly_agg.month_start = months.month_start
        LEFT JOIN net_median ON net_median.month_start = months.month_start
        LEFT JOIN total_median ON total_median.month_start = months.month_start
        ORDER BY months.month_start DESC
        """,
        (min_month_start, month_start, min_month_start, month_end),
    )
    monthly_rows = cur.fetchall()
    db.close()

    for row in rows:
        deliveries = row.get("deliveries") or 0
        total_yen = row.get("total_yen") or 0
        row["avg_yen"] = round(total_yen / deliveries) if deliveries else None

    deliveries_sum = summary.get("deliveries_sum") or 0
    total_sum = summary.get("total_sum") or 0
    summary_avg = round(total_sum / deliveries_sum) if deliveries_sum else None

    return render_template(
        "records/uber/list.html",
        rows=rows,
        monthly_rows=monthly_rows,
        default_work_date=today,
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
    return redirect(url_for("records.uber_list"))

def _handle_uber_upsert(redirect_endpoint: str):
    work_date = _parse_date(request.form.get("work_date", ""), "日付")
    deliveries = _parse_int(request.form.get("deliveries", ""), "件数")
    net_yen = _parse_int(request.form.get("net_yen", ""), "正味の料金")
    promo_yen = _parse_int(request.form.get("promo_yen", "0"), "プロモーション")
    other_yen = _parse_int(request.form.get("other_yen", "0"), "その他")
    tip_yen = _parse_int(request.form.get("tip_yen", "0"), "チップ")
    if None in (work_date, deliveries, net_yen, promo_yen, other_yen, tip_yen):
        return redirect(url_for(redirect_endpoint))
    if deliveries == 0 and net_yen == 0 and promo_yen == 0 and other_yen == 0 and tip_yen == 0:
        flash("件数が0で金額もすべて0のデータは登録できません。", "warning")
        return redirect(url_for(redirect_endpoint))

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
    return redirect(url_for(redirect_endpoint))


@records_bp.post("/uber")
@login_required
def uber_create_or_update():
    return _handle_uber_upsert("records.uber_list")


@records_bp.post("/uber/new")
@login_required
def uber_create():
    return _handle_uber_upsert("records.uber_list")


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
    return render_template("records/uber/form.html", item=item, default_work_date=item.get("work_date"))


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
    if deliveries == 0 and net_yen == 0 and promo_yen == 0 and other_yen == 0 and tip_yen == 0:
        flash("件数が0で金額もすべて0のデータは登録できません。", "warning")
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
    maintenance_items = list_maintenance_items(db=db)
    admin_items = (
        list_maintenance_items(include_inactive=True, db=db)
        if _is_admin_user()
        else []
    )
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT
            m.*,
            COALESCE(mi.name, m.item) AS item_name
        FROM bike_maintenance_log m
        LEFT JOIN maintenance_items mi ON mi.id = m.item_id
        ORDER BY event_date DESC, odometer_km DESC, id DESC
        """
    )
    rows = cur.fetchall()

    cur.execute(
        """
        SELECT
            mi.id AS item_id,
            mi.name AS item_name,
            mi.target_km,
            m.id AS log_id,
            m.event_date,
            m.odometer_km,
            m.note
        FROM maintenance_items mi
        LEFT JOIN bike_maintenance_log m
          ON m.id = (
            SELECT m2.id
            FROM bike_maintenance_log m2
            WHERE m2.item_id = mi.id
               OR (m2.item_id IS NULL AND m2.item = mi.name)
            ORDER BY m2.event_date DESC, m2.odometer_km DESC, m2.id DESC
            LIMIT 1
        )
        WHERE mi.is_active = 1
        ORDER BY mi.sort_order, mi.id
        """
    )
    latest_rows = cur.fetchall()
    db.close()

    current_odometer_raw = request.args.get("current_odometer", "").strip()
    current_odometer = _parse_int(
        current_odometer_raw, "現在メーター", allow_empty=True
    )
    current_odometer_value = (
        current_odometer_raw if current_odometer is not None or not current_odometer_raw else ""
    )
    summary_rows = []
    for row in latest_rows:
        has_log = row.get("log_id") is not None
        target_km = row.get("target_km")
        if not has_log:
            summary_rows.append(
                {
                    "item_name": row["item_name"],
                    "event_date": None,
                    "odometer_km": None,
                    "note": None,
                    "since_km": None,
                    "target_km": None,
                    "remaining_km": None,
                }
            )
            continue

        since_display = None
        remaining_display = None
        if current_odometer is not None:
            since_km = current_odometer - int(row["odometer_km"])
            since_display = since_km
            if target_km is not None:
                remaining_display = target_km - since_km

        summary_rows.append(
            {
                "item_name": row["item_name"],
                "event_date": row["event_date"],
                "odometer_km": row["odometer_km"],
                "note": row.get("note") or None,
                "since_km": since_display,
                "target_km": target_km,
                "remaining_km": remaining_display,
            }
        )

    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    return render_template(
        "records/maintenance/list.html",
        rows=rows,
        maintenance_items=maintenance_items,
        admin_items=admin_items,
        summary_rows=summary_rows,
        default_event_date=today,
        current_odometer=current_odometer_value,
        is_admin=_is_admin_user(),
    )


@records_bp.get("/maintenance/new")
@login_required
def maintenance_new():
    return redirect(url_for("records.maintenance_list"), code=302)


@records_bp.post("/maintenance")
@records_bp.post("/maintenance/new")
@login_required
def maintenance_create():
    event_date = _parse_date(request.form.get("event_date", ""), "日付")
    odometer_km = _parse_int(request.form.get("odometer_km", ""), "メーター")
    item_id = _parse_int(request.form.get("item_id", ""), "項目")
    note = request.form.get("note", "").strip() or None
    if event_date is None or odometer_km is None or item_id is None:
        return redirect(url_for("records.maintenance_list", _anchor="new"))

    now = now_ts()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT name FROM maintenance_items WHERE id = %s AND is_active = 1",
        (item_id,),
    )
    item_row = cur.fetchone()
    if not item_row:
        db.close()
        flash("項目を選択してください。", "warning")
        return redirect(url_for("records.maintenance_list", _anchor="new"))
    item_name = item_row[0]
    cur.execute(
        """
        INSERT INTO bike_maintenance_log (
            event_date,
            odometer_km,
            item_id,
            item,
            note,
            created_at,
            updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (event_date, odometer_km, item_id, item_name, note, now, now),
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
    cur.execute(
        """
        SELECT id, name, target_km, sort_order
        FROM maintenance_items
        WHERE is_active = 1
        ORDER BY sort_order, id
        """
    )
    maintenance_items = cur.fetchall()
    db.close()
    if not item:
        flash("対象の記録が見つかりません。", "warning")
        return redirect(url_for("records.maintenance_list"))
    if item.get("item_id") is None and item.get("item"):
        matched = next(
            (mi for mi in maintenance_items if mi["name"] == item["item"]),
            None,
        )
        if matched:
            item["item_id"] = matched["id"]
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    return render_template(
        "records/maintenance/form.html",
        item=item,
        maintenance_items=maintenance_items,
        default_event_date=today,
    )


@records_bp.post("/maintenance/<int:record_id>/edit")
@login_required
def maintenance_update(record_id: int):
    event_date = _parse_date(request.form.get("event_date", ""), "日付")
    odometer_km = _parse_int(request.form.get("odometer_km", ""), "メーター")
    item_id = _parse_int(request.form.get("item_id", ""), "項目")
    note = request.form.get("note", "").strip() or None
    if event_date is None or odometer_km is None or item_id is None:
        return redirect(url_for("records.maintenance_edit", record_id=record_id))

    now = now_ts()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT name FROM maintenance_items WHERE id = %s AND is_active = 1",
        (item_id,),
    )
    item_row = cur.fetchone()
    if not item_row:
        db.close()
        flash("項目を選択してください。", "warning")
        return redirect(url_for("records.maintenance_edit", record_id=record_id))
    item_name = item_row[0]
    cur.execute(
        """
        UPDATE bike_maintenance_log
        SET event_date = %s,
            odometer_km = %s,
            item_id = %s,
            item = %s,
            note = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (event_date, odometer_km, item_id, item_name, note, now, record_id),
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


@records_bp.post("/maintenance/items/add")
@login_required
def maintenance_item_add():
    resp = _require_admin_for_records()
    if resp is not None:
        return resp
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("項目名を入力してください。", "warning")
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    if len(name) > 191:
        flash("項目名が長すぎます。", "warning")
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    target_km_raw = (request.form.get("target_km") or "").strip()
    target_km = _parse_int(
        target_km_raw,
        "交換目安",
        allow_empty=True,
    )
    sort_order_raw = (request.form.get("sort_order") or "").strip()
    sort_order = _parse_int(
        sort_order_raw,
        "表示順",
        allow_empty=True,
    )
    if target_km_raw and target_km is None:
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    if sort_order_raw and sort_order is None:
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    if target_km is not None and target_km < 0:
        flash("交換目安は0以上で入力してください。", "warning")
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    if sort_order is not None and sort_order < 0:
        flash("表示順は0以上で入力してください。", "warning")
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    is_active = request.form.get("is_active") == "1"
    insert_maintenance_item(
        name=name,
        target_km=target_km,
        sort_order=sort_order,
        is_active=is_active,
    )
    flash("項目を追加しました。", "success")
    return redirect(url_for("records.maintenance_list", _anchor="item-admin"))


@records_bp.post("/maintenance/items/<int:item_id>/update")
@login_required
def maintenance_item_update(item_id: int):
    resp = _require_admin_for_records()
    if resp is not None:
        return resp
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("項目名を入力してください。", "warning")
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    if len(name) > 191:
        flash("項目名が長すぎます。", "warning")
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    target_km_raw = (request.form.get("target_km") or "").strip()
    target_km = _parse_int(
        target_km_raw,
        "交換目安",
        allow_empty=True,
    )
    sort_order_raw = (request.form.get("sort_order") or "").strip()
    sort_order = _parse_int(
        sort_order_raw,
        "表示順",
        allow_empty=False,
    )
    if target_km_raw and target_km is None:
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    if sort_order is None:
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    if target_km is not None and target_km < 0:
        flash("交換目安は0以上で入力してください。", "warning")
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    if sort_order is not None and sort_order < 0:
        flash("表示順は0以上で入力してください。", "warning")
        return redirect(url_for("records.maintenance_list", _anchor="item-admin"))
    is_active = request.form.get("is_active") == "1"
    update_maintenance_item(
        item_id=item_id,
        name=name,
        target_km=target_km,
        sort_order=sort_order,
        is_active=is_active,
    )
    flash("項目を更新しました。", "success")
    return redirect(url_for("records.maintenance_list", _anchor="item-admin"))


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
    for row in rows:
        km_per_l = None
        yen_per_km = None
        trip_km = row.get("trip_km")
        if trip_km is not None:
            trip_km_val = float(trip_km)
            liters = float(row["liters"])
            if trip_km_val > 0 and liters > 0:
                km_per_l = trip_km_val / liters
                if row.get("yen_per_liter") is not None:
                    yen_per_km = (row["yen_per_liter"] * liters) / trip_km_val
        row["km_per_l"] = km_per_l
        row["yen_per_km"] = yen_per_km
        computed.append(row)

    computed.sort(
        key=lambda r: (
            r["fill_date"],
            r["odometer_km"] or 0,
            r["id"],
        ),
        reverse=True,
    )

    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    return render_template(
        "records/fuel/list.html",
        rows=computed,
        default_fill_date=today,
    )


@records_bp.get("/fuel/new")
@login_required
def fuel_new():
    return redirect(url_for("records.fuel_list"))


@records_bp.post("/fuel/new")
@login_required
def fuel_create_legacy():
    return fuel_create()


@records_bp.post("/fuel")
@login_required
def fuel_create():
    fill_date = _parse_date(request.form.get("fill_date", ""), "日付")
    odometer_km = _round_decimal(
        _parse_decimal(
            request.form.get("odometer_km", ""),
            "メーター",
            allow_empty=True,
        ),
        1,
    )
    trip_km = _round_decimal(
        _parse_decimal(
            request.form.get("trip_km", ""),
            "トリップ",
            allow_empty=True,
        ),
        1,
    )
    liters = _parse_decimal(request.form.get("liters", ""), "給油量")
    yen_per_liter = _parse_int(
        request.form.get("yen_per_liter", ""),
        "円/L",
        allow_empty=True,
    )
    note = request.form.get("note", "").strip() or None
    is_full = 1 if request.form.get("is_full") == "on" else 0
    if fill_date is None or liters is None:
        return redirect(url_for("records.fuel_list"))
    if liters <= 0:
        flash("給油量は0より大きい値を入力してください。", "warning")
        return redirect(url_for("records.fuel_list"))
    if odometer_km is None and trip_km is None:
        flash("メーターかトリップを入力してください。", "warning")
        return redirect(url_for("records.fuel_list"))

    now = now_ts()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO bike_fuel_log (
            fill_date,
            odometer_km,
            trip_km,
            liters,
            yen_per_liter,
            is_full,
            note,
            created_at,
            updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            fill_date,
            odometer_km,
            trip_km,
            liters,
            yen_per_liter,
            is_full,
            note,
            now,
            now,
        ),
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
    return render_template("records/fuel/form.html", item=item)


@records_bp.post("/fuel/<int:record_id>/edit")
@login_required
def fuel_update(record_id: int):
    fill_date = _parse_date(request.form.get("fill_date", ""), "日付")
    odometer_km = _round_decimal(
        _parse_decimal(
            request.form.get("odometer_km", ""),
            "メーター",
            allow_empty=True,
        ),
        1,
    )
    trip_km = _round_decimal(
        _parse_decimal(
            request.form.get("trip_km", ""),
            "トリップ",
            allow_empty=True,
        ),
        1,
    )
    liters = _parse_decimal(request.form.get("liters", ""), "給油量")
    yen_per_liter = _parse_int(
        request.form.get("yen_per_liter", ""),
        "円/L",
        allow_empty=True,
    )
    note = request.form.get("note", "").strip() or None
    is_full = 1 if request.form.get("is_full") == "on" else 0
    if fill_date is None or liters is None:
        return redirect(url_for("records.fuel_edit", record_id=record_id))
    if liters <= 0:
        flash("給油量は0より大きい値を入力してください。", "warning")
        return redirect(url_for("records.fuel_edit", record_id=record_id))
    if odometer_km is None and trip_km is None:
        flash("メーターかトリップを入力してください。", "warning")
        return redirect(url_for("records.fuel_edit", record_id=record_id))

    now = now_ts()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        UPDATE bike_fuel_log
        SET fill_date = %s,
            odometer_km = %s,
            trip_km = %s,
            liters = %s,
            yen_per_liter = %s,
            is_full = %s,
            note = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (
            fill_date,
            odometer_km,
            trip_km,
            liters,
            yen_per_liter,
            is_full,
            note,
            now,
            record_id,
        ),
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
