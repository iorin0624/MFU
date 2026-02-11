from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from threading import Lock
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for

from app.utils.db import get_db

from .models import (
    ensure_records_schema,
    get_current_odometer_km,
    insert_maintenance_item,
    list_maintenance_items,
    now_ts,
    set_current_odometer_km,
    update_maintenance_item,
)

records_bp = Blueprint(
    "records",
    __name__,
    template_folder="templates",
    static_folder="static",
)

records_api_bp = Blueprint("records_api", __name__)


_schema_init_lock = Lock()
_schema_initialized = False
_uber_ocr_preview_lock = Lock()
_uber_ocr_preview_store: dict[str, dict[str, str | float]] = {}
_UBER_OCR_TMP_DIR = os.getenv("UBER_OCR_TMP_DIR", os.path.join("/tmp", "mfu", "uber_ocr"))
_UBER_OCR_PREVIEW_TTL_SEC = 60 * 60 * 24
_UBER_OCR_QUEUE_DIR = os.getenv("UBER_OCR_QUEUE_DIR", os.path.join("/tmp", "mfu", "uber_ocr_queue"))
_UBER_OCR_QUEUE_TTL_SEC = int(os.getenv("UBER_OCR_QUEUE_TTL_SEC", str(60 * 60 * 24)))


@records_bp.app_template_filter("fmt_yen")
def fmt_yen(value, digits=0):
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except Exception:
        return str(value)
    precision = int(digits)
    if precision > 0:
        return f"{round(number, precision):,.{precision}f}"
    return f"{round(number):,}"


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


def api_token_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        configured_token = os.getenv("UBER_OCR_API_TOKEN") or os.getenv("RECORDS_API_TOKEN")
        if not configured_token:
            return jsonify({"ok": False, "message": "APIトークンが未設定です。"}), 401

        authorization = request.headers.get("Authorization", "").strip()
        if not authorization:
            return jsonify({"ok": False, "message": "Authorizationヘッダーが必要です。"}), 401

        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return jsonify({"ok": False, "message": "Bearerトークン形式で指定してください。"}), 401

        if token != configured_token:
            return jsonify({"ok": False, "message": "トークンが不正です。"}), 403

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


def _cleanup_old_uber_ocr_files() -> None:
    now_ts_value = time.time()
    expired_tokens: list[str] = []
    with _uber_ocr_preview_lock:
        for token, item in _uber_ocr_preview_store.items():
            created_at = float(item.get("created_at", 0))
            path = str(item.get("path", ""))
            if now_ts_value - created_at > _UBER_OCR_PREVIEW_TTL_SEC or not os.path.exists(path):
                expired_tokens.append(token)
        for token in expired_tokens:
            path = str(_uber_ocr_preview_store[token].get("path", ""))
            _uber_ocr_preview_store.pop(token, None)
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass


def _normalize_ocr_int(value, label: str, warnings: list[str]) -> int:
    if value is None:
        warnings.append(f"{label}を読み取れなかったため0を設定しました。")
        return 0
    if isinstance(value, bool):
        warnings.append(f"{label}が不正な形式のため0を設定しました。")
        return 0
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    if not raw:
        warnings.append(f"{label}が空欄だったため0を設定しました。")
        return 0
    normalized = re.sub(r"[^0-9\-]", "", raw)
    if normalized in ("", "-"):
        warnings.append(f"{label}の値が不明なため0を設定しました。")
        return 0
    try:
        return int(normalized)
    except ValueError:
        warnings.append(f"{label}の数値化に失敗したため0を設定しました。")
        return 0


def _parse_ocr_work_date(value: str | None, warnings: list[str]) -> date:
    if not value:
        return datetime.now(ZoneInfo("Asia/Tokyo")).date()
    raw = str(value).strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        warnings.append("work_dateの形式が不正だったため、当日を設定しました。")
        return datetime.now(ZoneInfo("Asia/Tokyo")).date()


def _save_uber_ocr_upload(uploaded) -> tuple[str, str]:
    ext = os.path.splitext(uploaded.filename or "")[1].lower()
    if not ext:
        guessed_ext = mimetypes.guess_extension(uploaded.mimetype or "")
        ext = guessed_ext or ".png"
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"}:
        ext = ".png"

    filename = f"{int(time.time())}_{uuid.uuid4().hex}{ext}"
    image_path = os.path.join(_UBER_OCR_QUEUE_DIR, filename)
    os.makedirs(_UBER_OCR_QUEUE_DIR, exist_ok=True)
    uploaded.save(image_path)
    return image_path, ext


def _delete_file_safely(path: str | None) -> None:
    if not path:
        return
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _cleanup_old_uber_ocr_queue(db) -> None:
    ttl_sec = _UBER_OCR_QUEUE_TTL_SEC
    if ttl_sec <= 0:
        return
    threshold = datetime.now() - timedelta(seconds=ttl_sec)
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, image_path
        FROM uber_ocr_queue
        WHERE status = 'pending' AND created_at < %s
        """,
        (threshold,),
    )
    rows = cur.fetchall()
    if not rows:
        return
    for row in rows:
        _delete_file_safely(row.get("image_path"))
    cur = db.cursor()
    cur.execute(
        """
        DELETE FROM uber_ocr_queue
        WHERE status = 'pending' AND created_at < %s
        """,
        (threshold,),
    )
    db.commit()


def _analyze_uber_screenshot_with_openai(image_path: str, mime_type: str) -> dict:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が未設定です。")

    model = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")

    from openai import OpenAI

    with open(image_path, "rb") as f:
        encoded_image = base64.b64encode(f.read()).decode("utf-8")

    system_prompt = (
        "あなたはUberの売上スクリーンショットを解析するOCRアシスタントです。"
        "出力は必ずJSONオブジェクト1つのみとし、説明文は出力しないでください。"
    )
    user_prompt = (
        "日本語UIから以下を抽出してください: ポイント、正味の料金、プロモーション、"
        "その他の売り上げ、チップ。\n"
        "必ずこのJSON形式のみ返してください:\n"
        "{\n"
        '  "deliveries": <int or null>,\n'
        '  "net_yen": <int or null>,\n'
        '  "promo_yen": <int or null>,\n'
        '  "other_yen": <int or null>,\n'
        '  "tip_yen": <int or null>,\n'
        '  "notes": [<string>...]\n'
        "}\n"
        "通貨記号・カンマ・空白は除去して整数として解釈し、不明ならnull。"
    )

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{encoded_image}"},
                    },
                ],
            },
        ],
    )
    content = (response.choices[0].message.content or "{}").strip()
    return json.loads(content)


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


@records_bp.post("/uber/ocr")
@login_required
def uber_ocr_analyze():
    _cleanup_old_uber_ocr_files()

    uploaded = request.files.get("image")
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "message": "画像ファイルを選択してください。", "warnings": []}), 400

    ext = os.path.splitext(uploaded.filename)[1].lower()
    if not ext:
        guessed_ext = mimetypes.guess_extension(uploaded.mimetype or "")
        ext = guessed_ext or ".png"
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"}:
        ext = ".png"

    token = uuid.uuid4().hex
    filename = f"{int(time.time())}_{token}{ext}"
    image_path = os.path.join(_UBER_OCR_TMP_DIR, filename)

    warnings: list[str] = []
    try:
        os.makedirs(_UBER_OCR_TMP_DIR, exist_ok=True)
        uploaded.save(image_path)
        result = _analyze_uber_screenshot_with_openai(
            image_path=image_path,
            mime_type=uploaded.mimetype or "image/png",
        )
    except Exception as exc:
        if os.path.exists(image_path):
            try:
                os.remove(image_path)
            except OSError:
                pass
        return jsonify(
            {
                "ok": False,
                "message": "画像解析に失敗しました。OpenAI設定またはサーバー設定を確認してください。",
                "warnings": [f"詳細: {exc}", "手入力で保存は可能です。"],
            }
        )

    notes = result.get("notes") if isinstance(result, dict) else None
    if isinstance(notes, list):
        warnings.extend([str(note) for note in notes if str(note).strip()])

    fields = {
        "work_date": datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat(),
        "deliveries": _normalize_ocr_int(result.get("deliveries") if isinstance(result, dict) else None, "ポイント", warnings),
        "net_yen": _normalize_ocr_int(result.get("net_yen") if isinstance(result, dict) else None, "正味の料金", warnings),
        "promo_yen": _normalize_ocr_int(result.get("promo_yen") if isinstance(result, dict) else None, "プロモーション", warnings),
        "other_yen": _normalize_ocr_int(result.get("other_yen") if isinstance(result, dict) else None, "その他の売り上げ", warnings),
        "tip_yen": _normalize_ocr_int(result.get("tip_yen") if isinstance(result, dict) else None, "チップ", warnings),
    }

    with _uber_ocr_preview_lock:
        _uber_ocr_preview_store[token] = {"path": image_path, "created_at": time.time()}

    return jsonify(
        {
            "ok": True,
            "fields": fields,
            "warnings": warnings,
            "preview_url": url_for("records.uber_ocr_preview", token=token),
        }
    )


@records_bp.get("/uber/ocr-preview/<token>")
@login_required
def uber_ocr_preview(token: str):
    _cleanup_old_uber_ocr_files()

    with _uber_ocr_preview_lock:
        item = _uber_ocr_preview_store.get(token)
    if not item:
        abort(404)

    image_path = str(item.get("path", ""))
    if not image_path or not os.path.isfile(image_path):
        abort(404)
    return send_file(image_path)


@records_api_bp.post("/api/records/uber/ocr-queue")
@api_token_required
def uber_ocr_queue_enqueue_api():
    uploaded = request.files.get("image")
    if not uploaded or not uploaded.filename:
        return jsonify({"ok": False, "message": "imageファイルが必要です。", "warnings": []}), 400

    warnings: list[str] = []
    work_date = _parse_ocr_work_date(request.form.get("work_date"), warnings)
    image_path = ""
    db = None
    try:
        db = get_db()
        _cleanup_old_uber_ocr_queue(db)

        image_path, _ = _save_uber_ocr_upload(uploaded)
        result = _analyze_uber_screenshot_with_openai(
            image_path=image_path,
            mime_type=uploaded.mimetype or "image/png",
        )

        notes = result.get("notes") if isinstance(result, dict) else None
        if isinstance(notes, list):
            warnings.extend([str(note) for note in notes if str(note).strip()])

        fields = {
            "work_date": work_date.isoformat(),
            "deliveries": _normalize_ocr_int(result.get("deliveries") if isinstance(result, dict) else None, "ポイント", warnings),
            "net_yen": _normalize_ocr_int(result.get("net_yen") if isinstance(result, dict) else None, "正味の料金", warnings),
            "promo_yen": _normalize_ocr_int(result.get("promo_yen") if isinstance(result, dict) else None, "プロモーション", warnings),
            "other_yen": _normalize_ocr_int(result.get("other_yen") if isinstance(result, dict) else None, "その他の売り上げ", warnings),
            "tip_yen": _normalize_ocr_int(result.get("tip_yen") if isinstance(result, dict) else None, "チップ", warnings),
        }

        now = now_ts()
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO uber_ocr_queue (
                status,
                work_date,
                deliveries,
                net_yen,
                promo_yen,
                other_yen,
                tip_yen,
                warnings_json,
                image_path,
                mime_type,
                original_filename,
                created_at,
                updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "pending",
                work_date,
                fields["deliveries"],
                fields["net_yen"],
                fields["promo_yen"],
                fields["other_yen"],
                fields["tip_yen"],
                json.dumps(warnings, ensure_ascii=False),
                image_path,
                uploaded.mimetype,
                uploaded.filename,
                now,
                now,
            ),
        )
        queue_id = int(cur.lastrowid)
        db.commit()
        db.close()
        return jsonify({"ok": True, "queue_id": queue_id, "fields": fields, "warnings": warnings})
    except Exception as exc:
        if db is not None:
            db.close()
        _delete_file_safely(image_path)
        return (
            jsonify(
                {
                    "ok": False,
                    "message": "画像解析またはキュー登録に失敗しました。",
                    "warnings": warnings + [f"詳細: {exc}"],
                }
            ),
            500,
        )


@records_bp.get("/uber/ocr-queue")
@login_required
def uber_ocr_queue_list():
    db = get_db()
    _cleanup_old_uber_ocr_queue(db)
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT
            id,
            status,
            work_date,
            deliveries,
            net_yen,
            promo_yen,
            other_yen,
            tip_yen,
            warnings_json,
            created_at
        FROM uber_ocr_queue
        WHERE status = 'pending'
        ORDER BY created_at DESC, id DESC
        """
    )
    rows = cur.fetchall()
    db.close()

    for row in rows:
        raw = row.get("warnings_json")
        parsed: list[str] = []
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    parsed = [str(item) for item in data if str(item).strip()]
            except Exception:
                parsed = [str(raw)]
        row["warnings"] = parsed

    return render_template("records/uber/ocr_queue.html", rows=rows)


@records_bp.get("/uber/ocr-queue/<int:queue_id>/image")
@login_required
def uber_ocr_queue_image(queue_id: int):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT image_path FROM uber_ocr_queue WHERE id = %s", (queue_id,))
    row = cur.fetchone()
    db.close()
    if not row:
        abort(404)
    image_path = row.get("image_path")
    if not image_path or not os.path.isfile(image_path):
        abort(404)
    return send_file(image_path)


@records_bp.post("/uber/ocr-queue/<int:queue_id>/commit")
@login_required
def uber_ocr_queue_commit(queue_id: int):
    work_date = _parse_date(request.form.get("work_date", ""), "日付")
    deliveries = _parse_int(request.form.get("deliveries", ""), "件数")
    net_yen = _parse_int(request.form.get("net_yen", ""), "正味")
    promo_yen = _parse_int(request.form.get("promo_yen", ""), "プロモ")
    other_yen = _parse_int(request.form.get("other_yen", ""), "その他")
    tip_yen = _parse_int(request.form.get("tip_yen", ""), "チップ")
    if None in (work_date, deliveries, net_yen, promo_yen, other_yen, tip_yen):
        return redirect(url_for("records.uber_ocr_queue_list"))

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT id, status, image_path FROM uber_ocr_queue WHERE id = %s",
        (queue_id,),
    )
    row = cur.fetchone()
    if not row:
        db.close()
        flash("対象キューが見つかりません。", "warning")
        return redirect(url_for("records.uber_ocr_queue_list"))
    if row.get("status") != "pending":
        db.close()
        flash("このキューは既に処理済みです。", "warning")
        return redirect(url_for("records.uber_ocr_queue_list"))

    now = now_ts()
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
        (work_date, deliveries, net_yen, promo_yen, other_yen, tip_yen, now, now),
    )
    cur.execute(
        """
        UPDATE uber_ocr_queue
        SET status = 'saved',
            work_date = %s,
            deliveries = %s,
            net_yen = %s,
            promo_yen = %s,
            other_yen = %s,
            tip_yen = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (work_date, deliveries, net_yen, promo_yen, other_yen, tip_yen, now, queue_id),
    )
    db.commit()
    db.close()

    _delete_file_safely(row.get("image_path"))
    flash("保存しました", "success")
    return redirect(url_for("records.uber_ocr_queue_list"))


@records_bp.post("/uber/ocr-queue/<int:queue_id>/discard")
@login_required
def uber_ocr_queue_discard(queue_id: int):
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT status, image_path FROM uber_ocr_queue WHERE id = %s",
        (queue_id,),
    )
    row = cur.fetchone()
    if not row:
        db.close()
        flash("対象キューが見つかりません。", "warning")
        return redirect(url_for("records.uber_ocr_queue_list"))
    now = now_ts()
    cur = db.cursor()
    cur.execute(
        """
        UPDATE uber_ocr_queue
        SET status = 'discarded',
            updated_at = %s
        WHERE id = %s
        """,
        (now, queue_id),
    )
    db.commit()
    db.close()

    _delete_file_safely(row.get("image_path"))
    flash("破棄しました", "success")
    return redirect(url_for("records.uber_ocr_queue_list"))


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

    current_odometer = get_current_odometer_km()
    current_odometer_value = f"{current_odometer:.1f}"
    current_odometer_default = str(int(current_odometer))
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
        since_km = current_odometer - Decimal(row["odometer_km"])
        since_display = since_km
        if target_km is not None:
            remaining_display = Decimal(target_km) - since_km

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
        current_odometer_km=current_odometer_value,
        current_odometer_default=current_odometer_default,
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
        odometer_readonly=False,
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
    current_odometer_km = get_current_odometer_km()

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
        current_odometer_km=f"{current_odometer_km:.1f}",
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
    trip_km = _round_decimal(
        _parse_decimal(
            request.form.get("trip_km", ""),
            "トリップ",
            allow_empty=False,
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
    if fill_date is None or liters is None or trip_km is None:
        return redirect(url_for("records.fuel_list"))
    if liters <= 0:
        flash("給油量は0より大きい値を入力してください。", "warning")
        return redirect(url_for("records.fuel_list"))
    if trip_km <= 0:
        flash("トリップは0より大きい値を入力してください。", "warning")
        return redirect(url_for("records.fuel_list"))

    current_odometer_km = get_current_odometer_km()
    new_odometer_km = _round_decimal(current_odometer_km + trip_km, 1)

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
            new_odometer_km,
            trip_km,
            liters,
            yen_per_liter,
            is_full,
            note,
            now,
            now,
        ),
    )
    set_current_odometer_km(new_odometer_km, db=db)
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
    return render_template(
        "records/fuel/form.html",
        item=item,
        odometer_readonly=False,
    )


@records_bp.post("/fuel/odometer")
@login_required
def fuel_update_odometer():
    odometer_raw = request.form.get("current_odometer_km", "")
    odometer = _round_decimal(
        _parse_decimal(
            odometer_raw,
            "現在オドメーター",
            allow_empty=False,
        ),
        1,
    )
    if odometer is None:
        return redirect(url_for("records.fuel_list"))
    if odometer < 0:
        flash("現在オドメーターは0以上で入力してください。", "warning")
        return redirect(url_for("records.fuel_list"))
    set_current_odometer_km(odometer)
    flash("現在オドメーターを更新しました。", "success")
    return redirect(url_for("records.fuel_list"))


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
            allow_empty=False,
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
    if fill_date is None or liters is None or trip_km is None:
        return redirect(url_for("records.fuel_edit", record_id=record_id))
    if liters <= 0:
        flash("給油量は0より大きい値を入力してください。", "warning")
        return redirect(url_for("records.fuel_edit", record_id=record_id))
    if trip_km <= 0:
        flash("トリップは0より大きい値を入力してください。", "warning")
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
