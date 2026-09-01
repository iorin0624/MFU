# /mnt/mfu/app/s_u_calendar/routes.py
from __future__ import annotations

import os
import requests
import msal
import datetime as dt

from datetime import date, datetime, timedelta
from calendar import monthrange
from functools import wraps
from flask import (
    Blueprint, render_template, request, jsonify, current_app,
    redirect, url_for, abort, session, g, make_response, flash
)

from .month_update import (
    MonthUpdateValidationError,
    existing_comparable_values,
    parse_month_updates,
)

# 既存のDBユーティリティを使用（プロジェクトの実装に合わせて）
# 例: app/utils/db.py に get_db() がある前提
try:
    from app.utils.db import get_db
except Exception:
    # もしパスが違う場合は、環境に合わせて修正してください
    from app.db import get_db  # フォールバック

s_u_calendar_bp = Blueprint(
    "s_u_calendar",
    __name__,
    template_folder="template",
    static_folder="static"
)

# --- admin判定（MFU準拠：session["user"] が "admin"）-------------------------
def admin_required(view):
    @wraps(view)
    def _wrap(*args, **kwargs):
        user = session.get("user")
        if not user:
            # 未ログイン → ログイン画面へ（next付きで元URLへ戻れる）
            return redirect(url_for("login", next=(request.full_path or request.path or "/")))
        if user != "admin":
            # ログイン済みだが管理者ではない
            return "管理者のみアクセス可能", 403
        return view(*args, **kwargs)
    return _wrap
# ---------------------------------------------------------------------------
# ---- ユーティリティ -----------------------------------------------------------
JST_TZ = "Asia/Tokyo"

def _parse_ym(query):
    """?year=YYYY&month=MM を取得。未指定は今日の年月。"""
    today = date.today()
    y = int(request.args.get("year", today.year))
    m = int(request.args.get("month", today.month))
    # 正規化（1..12）
    while m < 1:
        y -= 1; m += 12
    while m > 12:
        y += 1; m -= 12
    return y, m

def _month_edges(year: int, month: int):
    """その月の開始/終了日（当月内）"""
    _, last_day = monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)
    return start, end

def _adjacent_month(year: int, month: int, delta: int):
    """delta=+1 / -1 で前後月"""
    m = month + delta
    y = year
    if m < 1:
        y -= 1; m += 12
    elif m > 12:
        y += 1; m -= 12
    return y, m

def _fetch_days(db, start_d: date, end_d: date, public_only: bool = True):
    cur = db.cursor(dictionary=True)
    if public_only:
        sql = """SELECT day_date, status, label FROM su_calendar_days
                 WHERE is_public=1 AND day_date BETWEEN %s AND %s"""
        cur.execute(sql, (start_d, end_d))
    else:
        sql = """SELECT day_date, status, label, is_public FROM su_calendar_days
                 WHERE day_date BETWEEN %s AND %s"""
        cur.execute(sql, (start_d, end_d))
    rows = cur.fetchall()
    cur.close()
    # dict: 'YYYY-MM-DD' -> row
    return {r["day_date"].strftime("%Y-%m-%d"): r for r in rows}

def _log_admin(db, ip: str, text: str):
    """既存 logs テーブルを想定。無ければ無視してもOK。"""
    try:
        cur = db.cursor()
        cur.execute("INSERT INTO logs (log_date, ip, log_text) VALUES (NOW(), %s, %s)", (ip, f"[SUCAL] {text}"))
        db.commit()
        cur.close()
    except Exception:
        current_app.logger.warning("write [SUCAL] log failed", exc_info=True)


def _to_year_month(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _ensure_su_calendar_month_flags_table(db) -> None:
    cur = db.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS su_calendar_month_flags (
              id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
              `year_month` CHAR(7) NOT NULL,
              show_closed_notice TINYINT(1) NOT NULL DEFAULT 0,
              created_at DATETIME NULL,
              updated_at DATETIME NULL,
              PRIMARY KEY (id),
              UNIQUE KEY uq_su_calendar_month_flags_year_month (`year_month`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        db.commit()
    finally:
        cur.close()


@s_u_calendar_bp.before_request
def _auto_apply_su_calendar_sql():
    """SUCカレンダーアクセス時に月フラグテーブルを自動作成（初回のみ）。"""
    if request.blueprint != "s_u_calendar":
        return

    if current_app.extensions.get("sucal_schema_ready"):
        return

    try:
        db = get_db()
        _ensure_su_calendar_month_flags_table(db)
        current_app.extensions["sucal_schema_ready"] = True
    except Exception:
        current_app.logger.warning("[SUCAL] ensure month flag table failed", exc_info=True)


def get_su_calendar_month_flag(year: int, month: int) -> dict | None:
    db = get_db()
    _ensure_su_calendar_month_flags_table(db)
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT id, `year_month`, show_closed_notice, created_at, updated_at
              FROM su_calendar_month_flags
             WHERE `year_month`=%s
             LIMIT 1
            """,
            (_to_year_month(year, month),),
        )
        return cur.fetchone()
    finally:
        cur.close()


def is_su_calendar_closed_notice_enabled(year: int, month: int) -> bool:
    rec = get_su_calendar_month_flag(year, month)
    return bool(rec and rec.get("show_closed_notice") == 1)


def upsert_su_calendar_month_flag(year: int, month: int, show_closed_notice: bool) -> None:
    db = get_db()
    _ensure_su_calendar_month_flags_table(db)
    cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO su_calendar_month_flags
                (`year_month`, show_closed_notice, created_at, updated_at)
            VALUES (%s, %s, NOW(), NOW())
            ON DUPLICATE KEY UPDATE
                show_closed_notice=VALUES(show_closed_notice),
                updated_at=NOW()
            """,
            (_to_year_month(year, month), 1 if show_closed_notice else 0),
        )
        db.commit()
    finally:
        cur.close()
# -----------------------------------------------------------------------------


# ---- 日本の祝日ユーティリティ（API優先、jpholidayはフォールバック） ----------
import time
from calendar import monthrange as _monthrange

# 依存は任意。requests が無ければ urllib を使う
try:
    import requests as _requests
except Exception:
    _requests = None

from urllib.request import urlopen as _urlopen
from urllib.error import URLError as _URLError, HTTPError as _HTTPError
import json as _json

# 任意: pip install jpholiday（API障害時のフォールバックとして使用）
try:
    import jpholiday as _jph
except Exception:
    _jph = None

_HOL_API_BASE = "https://api.national-holidays.jp"
_HOL_CACHE: dict[int, tuple[float, dict]] = {}  # year -> (fetched_at, { "YYYY-MM-DD": {"name":..., "type":...} })
_HOL_TTL_SEC = 24 * 60 * 60  # 24h キャッシュ

def _http_get_json(url: str, timeout: int = 5):
    """requests があればそれを、無ければ urllib を使って JSON を取得"""
    if _requests is not None:
        r = _requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    else:
        with _urlopen(url, timeout=timeout) as resp:
            # Pythonの標準ライブラリでJSONを読む
            return _json.load(resp)

def _load_holidays_year_from_api(year: int) -> dict:
    """APIから指定年の祝日を取得し dict[date] = {name,type} を返す（無ければ空）。"""
    url = f"{_HOL_API_BASE}/{year}"
    arr = _http_get_json(url, timeout=6)
    # 年指定は基本 JSON配列。万一オブジェクトが返ってきたら配列化して握りつぶす。
    if isinstance(arr, dict):
        arr = [arr]
    data = {}
    for row in arr or []:
        d = row.get("date")
        nm = row.get("name")
        tp = row.get("type")
        if d and nm:
            data[d] = {"name": nm, "type": tp}
    return data

def _load_holidays_year(year: int) -> dict:
    """キャッシュ付きで年単位の祝日表を返す。API優先、失敗時は jpholiday を使って補完。"""
    now = time.time()
    ent = _HOL_CACHE.get(year)
    if ent and now - ent[0] < _HOL_TTL_SEC:
        return ent[1]

    data = {}
    try:
        data = _load_holidays_year_from_api(year)
    except Exception as e:
        current_app.logger.warning("[SUCAL] holiday API failed for year %s: %r", year, e)

        # --- APIが失敗した場合のみ、jpholiday で最小限のフォールバックを構築 ---
        if _jph is not None:
            try:
                for m in range(1, 13):
                    last = _monthrange(year, m)[1]
                    for dd in range(1, last + 1):
                        dt = date(year, m, dd)
                        nm = _jph.is_holiday_name(dt)
                        if nm:
                            data[dt.strftime("%Y-%m-%d")] = {"name": nm, "type": "国民の祝日"}
            except Exception as e2:
                current_app.logger.warning("[SUCAL] jpholiday fallback failed: %r", e2)

    _HOL_CACHE[year] = (now, data)
    return data

def _holiday_info(d: date) -> tuple[bool, str | None]:
    """(is_holiday, holiday_name) を返す。API結果が最優先。"""
    table = _load_holidays_year(d.year)
    key = d.strftime("%Y-%m-%d")
    rec = table.get(key)
    if rec:
        return True, rec.get("name")
    return False, None
# -----------------------------------------------------------------------------

def _api_token_ok(req) -> bool:
    """X-API-KEY ヘッダ or ?token= が SUC_API_TOKEN と一致すればOK"""
    token = req.headers.get("X-API-KEY") or req.args.get("token")
    expected = (current_app.config.get("SUC_API_TOKEN")
                or os.environ.get("SUC_API_TOKEN"))
    return bool(expected) and (token == expected)

def admin_or_token_required(view):
    @wraps(view)
    def _wrap(*args, **kwargs):
        # 管理セッション or APIトークン
        if session.get("user") == "admin" or _api_token_ok(request):
            return view(*args, **kwargs)
        return jsonify({"error": "forbidden"}), 403
    return _wrap

# === Graph: free/busy 取得（最小）==============================================
TENANT = os.environ["GRAPH_TENANT_ID"]
CLIENT_ID = os.environ["GRAPH_CLIENT_ID"]
CLIENT_SECRET = os.environ["GRAPH_CLIENT_SECRET"]
UPN = os.environ["OUTLOOK_SYNC_UPN"]

def get_app_token():
    auth = msal.ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT}",
        client_credential=CLIENT_SECRET,
    )
    scope = ["https://graph.microsoft.com/.default"]
    result = auth.acquire_token_silent(scope, account=None) or auth.acquire_token_for_client(scopes=scope)
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description"))
    return result["access_token"]

def get_freebusy(date_from: dt.date, date_to: dt.date):
    token = get_app_token()
    url = f"https://graph.microsoft.com/v1.0/users/{UPN}/calendar/getSchedule"
    body = {
        "schedules": [UPN],
        "startTime": {"dateTime": f"{date_from}T00:00:00", "timeZone": "Tokyo Standard Time"},
        "endTime":   {"dateTime": f"{date_to}T23:59:59", "timeZone": "Tokyo Standard Time"},
        "availabilityViewInterval": 60
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": 'outlook.timezone="Tokyo Standard Time"',
    }
    r = requests.post(url, json=body, headers=headers, timeout=15)
    if r.status_code >= 400:
        try:
            err = r.json()
        except Exception:
            err = {"text": r.text}
        current_app.logger.error("Graph getSchedule error %s: %s", r.status_code, err)
        r.raise_for_status()  # ← 既存どおり
    return r.json()

def _debug_decode_token():
    tok = get_app_token()
    import base64, json
    payload = tok.split('.')[1] + '=='
    data = json.loads(base64.urlsafe_b64decode(payload))
    current_app.logger.info("GRAPH token roles=%s, appid=%s, tid=%s",
                            data.get("roles"), data.get("appid"), data.get("tid"))
# ==============================================================================

def _get_meta(db, key: str) -> str | None:
    """
    su_calendar_meta から k=key の v を取得。
    テーブルが無くても例外にせず None を返す安全版。
    """
    try:
        cur = db.cursor()
        # テーブル存在チェック（無い環境でも例外にしない）
        cur.execute("""
            SELECT 1 FROM information_schema.tables
             WHERE table_name='su_calendar_meta' LIMIT 1
        """)
        if not cur.fetchone():
            cur.close()
            return None
        cur.execute("SELECT v FROM su_calendar_meta WHERE k=%s LIMIT 1", (key,))
        row = cur.fetchone()
        cur.close()
        return (row[0] if row else None)
    except Exception:
        return None


def _get_last_sync_meta() -> tuple[str | None, str | None]:
    """
    直近同期のメタ情報を (last_sync_at, last_sync_range) で返す。
    どちらか（または両方）が無い場合は None。
    """
    try:
        db = get_db()
    except Exception:
        return (None, None)
    return (_get_meta(db, "last_sync_at"), _get_meta(db, "last_sync_range"))


# =============== Blueprint 全テンプレートへ自動注入（admin/公開の両方） =============
# このコンテキストプロセッサにより、同 Blueprint（s_u_calendar_bp）配下の
# すべての render_template へ、変数 last_sync_at / last_sync_range が自動で渡ります。
# =============== Blueprint 全テンプレートへ自動注入（admin/公開の両方） =============
# 既存の inject_last_sync_meta をこの定義で置き換え
@s_u_calendar_bp.context_processor
def inject_last_sync_meta():
    def _get_meta(db, key: str) -> str | None:
        try:
            cur = db.cursor()
            cur.execute("""
                SELECT 1 FROM information_schema.tables
                 WHERE table_name='su_calendar_meta' LIMIT 1
            """)
            if not cur.fetchone():
                cur.close()
                return None
            cur.execute("SELECT v FROM su_calendar_meta WHERE k=%s LIMIT 1", (key,))
            row = cur.fetchone()
            cur.close()
            return (row[0] if row else None)
        except Exception:
            return None

    def _get_last_sync_meta():
        try:
            db = get_db()
        except Exception:
            return (None, None)
        return (_get_meta(db, "last_sync_at"), _get_meta(db, "last_sync_range"))

    def _strip_tz(s: str | None) -> str | None:
        """
        'YYYY-MM-DD HH:MM:SS+0900' → 'YYYY-MM-DD HH:MM:SS'
        既にタイムゾーン無しや None の場合はそのまま返す
        """
        if not s:
            return s
        # 期待フォーマットなら先頭19文字（YYYY-MM-DD HH:MM:SS）だけ返す
        if len(s) >= 19 and s[4] == "-" and s[7] == "-" and s[13] == ":" and s[16] == ":":
            return s[:19]
        # 念のため、末尾の +hhmm / -hhmm があれば落とす
        if len(s) > 5 and (s[-5] in ("+", "-") and s[-4:].isdigit()):
            return s[:-5]
        return s

    last_sync_at_raw, last_sync_range = _get_last_sync_meta()
    last_sync_at_clean = _strip_tz(last_sync_at_raw)

    return {
        # ★ ここでタイムゾーン無しの文字列にしてテンプレへ渡す
        "last_sync_at": last_sync_at_clean,
        "last_sync_range": last_sync_range,
    }




# ========================= 公開：万年カレンダー ===============================

@s_u_calendar_bp.route("/", methods=["GET"])
def calendar_month():
    """公開・月間ビュー（万年カレンダー／日曜はじまり）。スマホ対応テンプレ使用。"""
    year, month = _parse_ym(request)
    start_d, end_d = _month_edges(year, month)

    db = get_db()
    rows = _fetch_days(db, start_d, end_d, public_only=True)  # key: 'YYYY-MM-DD'
    show_closed_notice = is_su_calendar_closed_notice_enabled(year, month)

    # 前後月
    py, pm = _adjacent_month(year, month, -1)
    ny, nm = _adjacent_month(year, month, +1)
    public_on_suc_subdomain = request.host.partition(":")[0].lower() == "suc.iori0624.jp"
    prev_public_url = (
        f"/?year={py}&month={pm}"
        if public_on_suc_subdomain
        else url_for("s_u_calendar.calendar_month", year=py, month=pm)
    )
    next_public_url = (
        f"/?year={ny}&month={nm}"
        if public_on_suc_subdomain
        else url_for("s_u_calendar.calendar_month", year=ny, month=nm)
    )

    # 1日ずつの配列を作ってテンプレに渡す
    days = []
    d = start_d
    while d <= end_d:
        key = d.strftime("%Y-%m-%d")
        info = rows.get(key)
        is_hol, hol_name = _holiday_info(d)
        days.append({
            "date": d,                           # datetime.date
            "key": key,                          # 'YYYY-MM-DD'
            "status": (info["status"] if info else "free"),
            "label": (info.get("label") if info else None),
            "w": d.weekday(),                    # 0=月..6=日（テンプレで土日色付けに使える）
            "is_holiday": is_hol,
            "holiday_name": hol_name,
        })
        d += timedelta(days=1)

    busy_days = [item for item in days if item["status"] == "busy"]

    return render_template(
        "calendar_month.html",
        year=year,
        month=month,
        start_d=start_d,
        end_d=end_d,
        first_w=(start_d.weekday() + 1) % 7,     # 日曜はじまり用の先頭空白数（Sun=0）
        days=days,                                # ← テンプレ側はこれを回す
        busy_days=busy_days,
        prev_year=py, prev_month=pm,
        next_year=ny, next_month=nm,
        prev_public_url=prev_public_url,
        next_public_url=next_public_url,
        today=date.today(),
        show_closed_notice=show_closed_notice,
    )

@s_u_calendar_bp.route("/mini", methods=["GET"])
def calendar_mini():
    """Retired lightweight view: keep old bookmarks working."""
    today_d = date.today()
    return redirect(
        url_for(
            "s_u_calendar.calendar_month",
            year=today_d.year,
            month=today_d.month,
        ),
        code=302,
    )

@s_u_calendar_bp.route("/api/v1/days", methods=["GET"])
def api_days():
    """公開API：範囲の日付ステータス（is_public=1のみ）。"""
    # 期間は最大365日
    fmt = "%Y-%m-%d"
    today_d = date.today()
    from_s = request.args.get("from")
    to_s = request.args.get("to")

    if not from_s and not to_s:
        # 未指定 → 今月
        y, m = today_d.year, today_d.month
        start_d, end_d = _month_edges(y, m)
    else:
        try:
            start_d = datetime.strptime(from_s, fmt).date() if from_s else today_d
            end_d = datetime.strptime(to_s, fmt).date() if to_s else (start_d + timedelta(days=30))
        except ValueError:
            return jsonify({"error": "invalid date format (YYYY-MM-DD)"}), 400

    if (end_d - start_d).days > 365:
        return jsonify({"error": "range too large (<=365 days)"}), 400

    db = get_db()
    rows = _fetch_days(db, start_d, end_d, public_only=True)

    # レスポンス整形（空きも返すため全日を埋める）
    out = []
    d = start_d
    while d <= end_d:
        key = d.strftime("%Y-%m-%d")
        info = rows.get(key)
        if info:
            item = {"date": key, "status": info["status"]}
            if info.get("label"):
                item["label"] = info["label"]
        else:
            item = {"date": key, "status": "free"}
        out.append(item)
        d += timedelta(days=1)

    resp = jsonify({
        "range": {"from": start_d.strftime(fmt), "to": end_d.strftime(fmt)},
        "days": out,
        "tz": JST_TZ,
        "generated_at": datetime.now().isoformat(timespec="seconds")
    })
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp

# ========================= 管理：adminのみ ==================================

@s_u_calendar_bp.route("/admin", methods=["GET"])
@admin_required
def admin_index():
    """管理月間UI（同じ万年カレンダーを編集モードで）。"""
    year, month = _parse_ym(request)
    start_d, end_d = _month_edges(year, month)

    db = get_db()
    rows = _fetch_days(db, start_d, end_d, public_only=False)  # dict
    show_closed_notice = is_su_calendar_closed_notice_enabled(year, month)

    py, pm = _adjacent_month(year, month, -1)
    ny, nm = _adjacent_month(year, month, +1)

    days = []
    d = start_d
    while d <= end_d:
        key = d.strftime("%Y-%m-%d")
        info = rows.get(key) or {}
        is_hol, hol_name = _holiday_info(d)
        days.append({
            "date": d,
            "key": key,
            "status": info.get("status", "free"),
            "label": info.get("label"),
            "is_public": info.get("is_public", 1),
            "w": d.weekday(),               # 0=月..6=日
            "is_holiday": is_hol,
            "holiday_name": hol_name,
        })
        d += timedelta(days=1)

    return render_template(
        "calendar_admin.html",
        year=year, month=month,
        start_d=start_d, end_d=end_d,
        first_w=(start_d.weekday() + 1) % 7,  # 日曜はじまり
        days=days,                  # ← これを回す
        prev_year=py, prev_month=pm,
        next_year=ny, next_month=nm,
        today=date.today(),
        show_closed_notice=show_closed_notice,
    )


@s_u_calendar_bp.route("/admin/month_flag", methods=["POST"])
@admin_required
def admin_upsert_month_flag():
    year = int(request.form.get("year", 0))
    month = int(request.form.get("month", 0))
    if not (1 <= month <= 12 and 1 <= year <= 9999):
        abort(400)

    show_closed_notice = (request.form.get("show_closed_notice") == "1")
    upsert_su_calendar_month_flag(year, month, show_closed_notice)
    flash("利用者向け案内表示を更新しました。")
    return redirect(url_for("s_u_calendar.admin_index", year=year, month=month))

# === 1) 個別日付 更新/作成（フォームPOST） ================================
@s_u_calendar_bp.route("/admin/day", methods=["POST"])
@admin_required
def admin_upsert_day():
    """
    フォーム項目:
      - date: YYYY-MM-DD
      - status: "busy" | "free"
      - label: str (任意・空可)
      - is_public: "1" (on) | (無)
    仕様:
      - 既存行があれば UPDATE、なければ INSERT
      - 手動編集は synced_busy=0 とし、同期(clear_missing)で消されない
    """
    from datetime import datetime

    fmt = "%Y-%m-%d"
    v = request.form

    date_s    = (v.get("date") or "").strip()
    status    = (v.get("status") or "").strip() or "free"
    label_raw = v.get("label")
    label     = (label_raw if label_raw is not None else "").strip()
    is_public = 1 if str(v.get("is_public", "")).strip() == "1" else 0

    # バリデーション
    try:
        day_d = datetime.strptime(date_s, fmt).date()
    except Exception:
        return jsonify({"error": "invalid date"}), 400
    if status not in ("busy", "free"):
        return jsonify({"error": "invalid status"}), 400
    if len(label) > 100:
        return jsonify({"error": "label too long"}), 400

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT id FROM su_calendar_days WHERE day_date=%s", (day_d,))
        row = cur.fetchone()
        if row:
            # 既存 → 手動編集: synced_busy=0 に落とす
            cur.execute(
                """
                UPDATE su_calendar_days
                   SET status=%s,
                       label=%s,
                       is_public=%s,
                       synced_busy=0
                 WHERE day_date=%s
                """,
                (status, (label or None), is_public, day_d),
            )
        else:
            # 新規 → 手動作成: synced_busy=0 で登録
            cur.execute(
                """
                INSERT INTO su_calendar_days (day_date, status, label, is_public, synced_busy)
                VALUES (%s, %s, %s, %s, 0)
                """,
                (day_d, status, (label or None), is_public),
            )
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"error": "db_error", "reason": repr(e)}), 500
    finally:
        try:
            cur.close()
        except Exception:
            pass

    # 画面遷移（元ページへ）
    ref = request.headers.get("Referer") or url_for("s_u_calendar.admin_index", year=day_d.year, month=day_d.month)
    return redirect(ref)


@s_u_calendar_bp.route("/admin/month_days", methods=["POST"])
@admin_required
def admin_upsert_month_days():
    """Atomically save the changed day settings for the displayed month."""
    wants_json = (
        request.headers.get("X-Requested-With") == "XMLHttpRequest"
        or request.accept_mimetypes.best == "application/json"
    )

    try:
        year = int(request.form.get("year", 0))
        month = int(request.form.get("month", 0))
        updates = parse_month_updates(request.form, year=year, month=month)
    except (TypeError, ValueError, MonthUpdateValidationError) as exc:
        message = str(exc) or "入力内容を確認してください。"
        date_value = getattr(exc, "date_value", "")
        if wants_json:
            return jsonify(
                ok=False,
                error="validation_error",
                message=message,
                date=date_value,
            ), 400
        flash((f"{date_value}: " if date_value else "") + message, "danger")
        return redirect(request.referrer or url_for("s_u_calendar.admin_index"))

    db = get_db()
    cur = db.cursor(dictionary=True)
    changed_count = 0
    changed_dates: list[str] = []
    try:
        cur.execute(
            """
            SELECT day_date, status, label, is_public
              FROM su_calendar_days
             WHERE day_date BETWEEN %s AND %s
             FOR UPDATE
            """,
            (updates[0].day_date, updates[-1].day_date),
        )
        existing_rows = {}
        for row in cur.fetchall():
            row_date = row.get("day_date")
            key = row_date.isoformat() if hasattr(row_date, "isoformat") else str(row_date)
            existing_rows[key] = row

        for update in updates:
            current_row = existing_rows.get(update.date_key)
            if existing_comparable_values(current_row) == update.comparable_values:
                continue

            if current_row:
                cur.execute(
                    """
                    UPDATE su_calendar_days
                       SET status=%s,
                           label=%s,
                           is_public=%s,
                           synced_busy=0
                     WHERE day_date=%s
                    """,
                    (
                        update.status,
                        update.label or None,
                        update.is_public,
                        update.day_date,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO su_calendar_days
                        (day_date, status, label, is_public, synced_busy)
                    VALUES (%s, %s, %s, %s, 0)
                    """,
                    (
                        update.day_date,
                        update.status,
                        update.label or None,
                        update.is_public,
                    ),
                )
            changed_count += 1
            changed_dates.append(update.date_key)

        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception(
            "[SUCAL] month save failed year=%s month=%s", year, month
        )
        if wants_json:
            return jsonify(
                ok=False,
                error="db_error",
                message="保存に失敗しました。時間をおいて再度お試しください。",
            ), 500
        flash("保存に失敗しました。時間をおいて再度お試しください。", "danger")
        return redirect(url_for("s_u_calendar.admin_index", year=year, month=month))
    finally:
        try:
            cur.close()
        except Exception:
            pass

    _log_admin(
        db,
        request.remote_addr or "-",
        f"MONTH_SAVE {year:04d}-{month:02d} changed={changed_count}",
    )
    message = f"{changed_count}日分を保存しました。" if changed_count else "変更はありませんでした。"
    redirect_url = url_for("s_u_calendar.admin_index", year=year, month=month)
    if wants_json:
        return jsonify(
            ok=True,
            changed_count=changed_count,
            changed_dates=changed_dates,
            message=message,
            redirect_url=redirect_url,
        )
    flash(message, "success")
    return redirect(redirect_url)


# === 2) 範囲一括設定（フォームPOST） ======================================
@s_u_calendar_bp.route("/admin/bulk", methods=["POST"])
@admin_required
def admin_bulk():
    """
    フォーム項目:
      - from: YYYY-MM-DD
      - to  : YYYY-MM-DD
      - status: "busy" | "free"
      - label: str (任意・空可)
      - is_public: "1" (on) | (無)

    仕様:
      - 範囲内を手動で一括更新/作成
      - 既存は UPDATE、未存在は INSERT
      - 手動編集は synced_busy=0 とし同期の free 化対象から除外
    """
    from datetime import datetime, timedelta

    fmt = "%Y-%m-%d"
    v = request.form

    from_s   = (v.get("from") or "").strip()
    to_s     = (v.get("to") or "").strip()
    status   = (v.get("status") or "").strip() or "busy"
    label    = ((v.get("label") or "").strip())
    is_pub   = 1 if str(v.get("is_public", "")).strip() == "1" else 0

    # バリデーション
    try:
        d0 = datetime.strptime(from_s, fmt).date()
        d1 = datetime.strptime(to_s,   fmt).date()
    except Exception:
        return jsonify({"error": "invalid range"}), 400
    if d1 < d0:
        return jsonify({"error": "range reversed"}), 400
    if status not in ("busy", "free"):
        return jsonify({"error": "invalid status"}), 400
    if len(label) > 100:
        return jsonify({"error": "label too long"}), 400

    db = get_db()
    cur = db.cursor()
    try:
        d = d0
        while d <= d1:
            cur.execute("SELECT id FROM su_calendar_days WHERE day_date=%s", (d,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    UPDATE su_calendar_days
                       SET status=%s,
                           label=%s,
                           is_public=%s,
                           synced_busy=0
                     WHERE day_date=%s
                    """,
                    (status, (label or None), is_pub, d),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO su_calendar_days (day_date, status, label, is_public, synced_busy)
                    VALUES (%s, %s, %s, %s, 0)
                    """,
                    (d, status, (label or None), is_pub),
                )
            d += timedelta(days=1)

        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"error": "db_error", "reason": repr(e)}), 500
    finally:
        try:
            cur.close()
        except Exception:
            pass

    # 画面遷移（表示月へ）
    ref = request.headers.get("Referer") or url_for("s_u_calendar.admin_index", year=d0.year, month=d0.month)
    return redirect(ref)

@s_u_calendar_bp.route("/admin/quick_add", methods=["POST"])
@admin_required
def admin_quick_add():
    """単日クイック登録: yyyymmdd（デフォルト=busy）, label(任意), is_public"""
    ymd = (request.form.get("yyyymmdd") or "").strip()
    label = request.form.get("label") or None
    is_public = 1 if request.form.get("is_public", "1") in ("1", "true", "on") else 0

    # yyyymmdd バリデーション
    if not (len(ymd) == 8 and ymd.isdigit()):
        abort(400, "invalid yyyymmdd")
    try:
        d = datetime.strptime(ymd, "%Y%m%d").date()
    except Exception:
        abort(400, "invalid yyyymmdd")

    status = "busy"  # デフォルト：予定あり

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM su_calendar_days WHERE day_date=%s", (d,))
    row = cur.fetchone()
    if row:
        sql = "UPDATE su_calendar_days SET status=%s, label=%s, is_public=%s WHERE day_date=%s"
        cur.execute(sql, (status, label, is_public, d))
    else:
        sql = "INSERT INTO su_calendar_days (day_date, status, label, is_public) VALUES (%s,%s,%s,%s)"
        cur.execute(sql, (d, status, label, is_public))
    db.commit()
    cur.close()

    ip_for_log = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                  or request.remote_addr or "-")
    _log_admin(db, ip_for_log, f"QUICK_ADD {d.isoformat()} status={status} label={label or '-'} public={is_public}")

    # 追加した日の年月に戻る
    return redirect(url_for(".admin_index", year=d.year, month=d.month))

@s_u_calendar_bp.route("/admin/api/v1/quick_add", methods=["GET", "POST"])
@admin_or_token_required
def admin_api_quick_add():
    """ショートカット/CLI向け：単発 upsert。
    例：
      GET/POST /suc/admin/api/v1/quick_add?yyyymmdd=20251022&label=打合せ&status=busy&public=1&token=...
      ヘッダ X-API-KEY でも可
    """
    ds = (request.values.get("date") or request.values.get("yyyymmdd") or "").strip()
    status = (request.values.get("status") or "busy").strip().lower()
    label = (request.values.get("label") or "").strip() or None
    is_public = 1 if str(request.values.get("public", "1")).lower() in ("1", "true", "on", "yes") else 0

    if not ds:
        return jsonify({"error": "date or yyyymmdd is required"}), 400
    # parse date
    try:
        if len(ds) == 8 and ds.isdigit():
            d = datetime.strptime(ds, "%Y%m%d").date()
        else:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"error": "invalid date format"}), 400

    if status not in ("free", "busy"):
        status = "busy"  # デフォルト

    # ラベルがあれば free → busy に自動昇格（管理UIと同じポリシー）
    if label and status == "free":
        status = "busy"

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM su_calendar_days WHERE day_date=%s", (d,))
    row = cur.fetchone()
    if row:
        cur.execute("UPDATE su_calendar_days SET status=%s, label=%s, is_public=%s WHERE day_date=%s",
                    (status, label, is_public, d))
        action = "update"
    else:
        cur.execute("INSERT INTO su_calendar_days (day_date, status, label, is_public) VALUES (%s,%s,%s,%s)",
                    (d, status, label, is_public))
        action = "insert"
    db.commit()
    cur.close()

    ip_for_log = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                  or request.remote_addr or "-")
    _log_admin(db, ip_for_log, f"API_QUICK_ADD {d.isoformat()} status={status} label={label or '-'} public={is_public}")

    return jsonify({
        "result": "ok",
        "action": action,
        "saved": {
            "date": d.strftime("%Y-%m-%d"),
            "status": status,
            "label": label,
            "is_public": is_public
        }
    }), 200


# === Outlook → MFU 同期（指定範囲・62日分割対応） ===============================
@s_u_calendar_bp.route("/admin/sync_outlook_range", methods=["POST", "GET"])
@admin_required
def sync_outlook_range():
    """
    Outlook（Graph）から指定範囲の予定を取得し、MFUカレンダーへ反映。

    受け付け（GET/POST 共通）:
      A) from=YYYY-MM-DD, to=YYYY-MM-DD
      B) ym=YYYY-MM, months=1..6
      C) from_year=YYYY, from_month=MM, months=1..6

    オプション:
      - clear_missing: 1/true で範囲内で Outlook 側に存在しない日を free に戻す（synced_busy=1 の日だけ）
      - clear_labels : 1/true で上記 free 化時にラベルも消す
    """
    from datetime import datetime, date, timedelta
    from calendar import monthrange

    fmt = "%Y-%m-%d"
    v = request.values  # GET/POST 両対応
    MAX_WINDOW_DAYS = 62  # Graph FreeBusyViewOptions.TimeWindow 制限に合わせる

    # ---- helpers ------------------------------------------------------------
    def _month_edges(y: int, m: int) -> tuple[date, date]:
        _, lastd = monthrange(y, m)
        return date(y, m, 1), date(y, m, lastd)

    def _to_dt(iso_s: str) -> datetime:
        s = iso_s.replace("Z", "")
        try:
            return datetime.fromisoformat(s)
        except Exception:
            # 小数秒・タイムゾーン付きの保険
            if "." in s:
                try:
                    return datetime.fromisoformat(s.split(".", 1)[0])
                except Exception:
                    pass
            if "+" in s:
                try:
                    return datetime.fromisoformat(s.split("+", 1)[0])
                except Exception:
                    pass
            # 最後の保険（落ちたら上位で握る）
            return datetime.fromisoformat(s)

    def _as_bool(x, default=False) -> bool:
        if x is None:
            return default
        return str(x).strip().lower() in ("1", "true", "yes", "on")

    def _iter_windows(d0: date, d1: date, max_days: int):
        """[d0..d1] を max_days 以内の連続区間に分割して yield"""
        cur = d0
        delta = timedelta(days=max_days - 1)  # 例: 62日 → +61日 = 62日幅
        while cur <= d1:
            end = min(cur + delta, d1)
            yield (cur, end)
            cur = end + timedelta(days=1)

    # ---- 期間解決 -----------------------------------------------------------
    from_s = (v.get("from") or "").strip()
    to_s   = (v.get("to") or "").strip()
    ym_s   = (v.get("ym") or "").strip()
    fy_s   = (v.get("from_year") or "").strip()
    fm_s   = (v.get("from_month") or "").strip()

    months_raw = (v.get("months") or "").strip()
    try:
        months = int(months_raw) if months_raw else 0
    except ValueError:
        months = 0

    try:
        if from_s and to_s:
            start_d = datetime.strptime(from_s, fmt).date()
            end_d   = datetime.strptime(to_s,   fmt).date()
            if end_d < start_d:
                return jsonify({"error": "range reversed"}), 400

        elif ym_s:
            try:
                y, m = map(int, ym_s.split("-", 1))
                if not (1 <= m <= 12):
                    raise ValueError
            except Exception:
                return jsonify({"error": "invalid ym"}), 400

            if not (1 <= months <= 6):
                return jsonify({"error": "months must be 1..6"}), 400

            s0, _ = _month_edges(y, m)
            yy, mm = y, m + (months - 1)
            yy += (mm - 1) // 12
            mm = ((mm - 1) % 12) + 1
            _, lastd = monthrange(yy, mm)
            e0 = date(yy, mm, lastd)
            start_d, end_d = s0, e0

        elif fy_s and fm_s:
            try:
                y = int(fy_s); m = int(fm_s)
                if not (1 <= m <= 12):
                    raise ValueError
            except Exception:
                return jsonify({"error": "invalid from_year/from_month"}), 400

            if not (1 <= months <= 6):
                return jsonify({"error": "months must be 1..6"}), 400

            s0, _ = _month_edges(y, m)
            yy, mm = y, m + (months - 1)
            yy += (mm - 1) // 12
            mm = ((mm - 1) % 12) + 1
            _, lastd = monthrange(yy, mm)
            e0 = date(yy, mm, lastd)
            start_d, end_d = s0, e0

        else:
            return jsonify({"error": "specify from/to or ym+months"}), 400

    except Exception as e:
        return jsonify({"error": "bad range", "reason": repr(e)}), 400

    # ---- オプション ---------------------------------------------------------
    clear_missing = _as_bool(v.get("clear_missing"), False)
    clear_labels  = _as_bool(v.get("clear_labels"),  False)

    # ---- Graph 取得（62日分割で統合） ---------------------------------------
    busy_days: set[str] = set()
    windows = list(_iter_windows(start_d, end_d, MAX_WINDOW_DAYS))

    try:
        for (ws, we) in windows:
            try:
                data = get_freebusy(ws, we)
            except Exception as e:
                return jsonify({
                    "error": "graph_error",
                    "reason": repr(e),
                    "window": {"from": ws.strftime(fmt), "to": we.strftime(fmt)}
                }), 502

            try:
                items = (data.get("value") or [{}])[0].get("scheduleItems", []) or []
            except Exception as e:
                return jsonify({
                    "error": "graph_payload_unexpected",
                    "reason": repr(e),
                    "window": {"from": ws.strftime(fmt), "to": we.strftime(fmt)},
                    "raw_keys": list(data.keys()) if isinstance(data, dict) else str(type(data))
                }), 502

            for it in items:
                try:
                    st = _to_dt(it["start"]["dateTime"]).date()
                    en = _to_dt(it["end"]["dateTime"]).date()
                except Exception:
                    continue
                d_last = (en - timedelta(days=1)) if en > st else st
                d = max(st, ws)
                while d <= min(we, d_last):
                    if start_d <= d <= end_d:  # 念のため全体範囲でクリップ
                        busy_days.add(d.strftime(fmt))
                    d += timedelta(days=1)
    except Exception as e:
        return jsonify({"error": "expand_error", "reason": repr(e)}), 500

    # ---- DB 反映 ------------------------------------------------------------
    db = get_db()
    cur = db.cursor()
    try:
        # busy upsert
        for key in busy_days:
            d = datetime.strptime(key, fmt).date()
            cur.execute("SELECT id FROM su_calendar_days WHERE day_date=%s", (d,))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE su_calendar_days
                       SET status=%s, is_public=%s, synced_busy=1
                     WHERE day_date=%s
                """, ("busy", 1, d))
            else:
                cur.execute("""
                    INSERT INTO su_calendar_days (day_date, status, label, is_public, synced_busy)
                    VALUES (%s,%s,%s,%s,%s)
                """, (d, "busy", None, 1, 1))

        # 欠落分の free 化（手動 busy は保持）
        cleared = 0
        if clear_missing:
            d = start_d
            while d <= end_d:
                k = d.strftime(fmt)
                if k not in busy_days:
                    if clear_labels:
                        cur.execute("""
                            UPDATE su_calendar_days
                               SET status='free', label=NULL, synced_busy=0
                             WHERE day_date=%s AND synced_busy=1
                        """, (d,))
                    else:
                        cur.execute("""
                            UPDATE su_calendar_days
                               SET status='free', synced_busy=0
                             WHERE day_date=%s AND synced_busy=1
                        """, (d,))
                    cleared += (cur.rowcount or 0)
                d += timedelta(days=1)

        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"error": "db_error", "reason": repr(e)}), 500
    finally:
        try:
            cur.close()
        except Exception:
            pass

    # ---- 管理ログ -----------------------------------------------------------
    try:
        ip_for_log = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                      or request.remote_addr or "-")
        _log_admin(
            db, ip_for_log,
            f"SYNC_OUTLOOK_RANGE {start_d.isoformat()}..{end_d.isoformat()} "
            f"busy={len(busy_days)} clear_missing={int(clear_missing)} windows={len(windows)}"
        )
    except Exception:
        pass

    return jsonify({
        "result": "ok",
        "range": {"from": start_d.strftime(fmt), "to": end_d.strftime(fmt)},
        "busy_days": sorted(busy_days),
        "count": len(busy_days),
        "cleared": (cleared if clear_missing else 0),
        "options": {"clear_missing": clear_missing, "clear_labels": clear_labels},
        "windows": [{"from": a.strftime(fmt), "to": b.strftime(fmt)} for (a, b) in windows]
    }), 200


# === 当月から6ヶ月分 同期API（GET/トークン対応・62日分割・最終同期記録） ========
@s_u_calendar_bp.route("/admin/api/v1/sync_outlook_6m", methods=["GET"])
@admin_or_token_required
def admin_api_sync_outlook_6m():
    from datetime import datetime, date, timedelta, timezone
    from calendar import monthrange

    MAX_WINDOW_DAYS = 62
    fmt = "%Y-%m-%d"
    q = request.args

    # ----- helpers -----------------------------------------------------------
    def _as_bool(x, default=False) -> bool:
        if x is None:
            return default
        return str(x).strip().lower() in ("1", "true", "yes", "on")

    def _month_edges(y: int, m: int) -> tuple[date, date]:
        _, lastd = monthrange(y, m)
        return date(y, m, 1), date(y, m, lastd)

    def _iter_windows(d0: date, d1: date, max_days: int):
        cur = d0
        delta = timedelta(days=max_days - 1)  # 62日→ +61 で62日幅
        while cur <= d1:
            end = min(cur + delta, d1)
            yield (cur, end)
            cur = end + timedelta(days=1)

    def _to_dt(iso_s: str) -> datetime:
        s = iso_s.replace("Z", "")
        try:
            return datetime.fromisoformat(s)
        except Exception:
            if "." in s:
                try:
                    return datetime.fromisoformat(s.split(".", 1)[0])
                except Exception:
                    pass
            if "+" in s:
                try:
                    return datetime.fromisoformat(s.split("+", 1)[0])
                except Exception:
                    pass
            return datetime.fromisoformat(s)

    # ----- JSTの当月 → 6ヶ月目末日 -----------------------------------------
    jst = timezone(timedelta(hours=9))
    now_jst = datetime.now(jst)
    today = now_jst.date()
    y, m = today.year, today.month
    start_d, _ = _month_edges(y, m)

    yy, mm = y, m + 5  # 当月を1として+5ヶ月 → 合計6ヶ月分
    yy += (mm - 1) // 12
    mm = ((mm - 1) % 12) + 1
    _, lastd = monthrange(yy, mm)
    end_d = date(yy, mm, lastd)

    clear_missing = _as_bool(q.get("clear_missing"), False)
    clear_labels  = _as_bool(q.get("clear_labels"), False)

    # ----- Graph 取得（62日分割で結合） -------------------------------------
    busy_days: set[str] = set()
    windows = list(_iter_windows(start_d, end_d, MAX_WINDOW_DAYS))
    try:
        for (ws, we) in windows:
            data = get_freebusy(ws, we)  # 既存のGraphヘルパを利用
            items = (data.get("value") or [{}])[0].get("scheduleItems", []) or []
            for it in items:
                try:
                    st = _to_dt(it["start"]["dateTime"]).date()
                    en = _to_dt(it["end"]["dateTime"]).date()
                except Exception:
                    continue
                # Graphの終了は非包含 → 日単位に正規化
                d_last = (en - timedelta(days=1)) if en > st else st
                d = max(st, ws)
                while d <= min(we, d_last):
                    if start_d <= d <= end_d:
                        busy_days.add(d.strftime(fmt))
                    d += timedelta(days=1)
    except Exception as e:
        return jsonify({"error": "graph_error", "reason": repr(e)}), 502

    # ----- DB反映 ------------------------------------------------------------
    db = get_db()
    cur = db.cursor()
    try:
        # busy upsert（synced_busy=1 に設定）
        for key in busy_days:
            d = datetime.strptime(key, fmt).date()
            cur.execute("SELECT id FROM su_calendar_days WHERE day_date=%s", (d,))
            row = cur.fetchone()
            if row:
                cur.execute("""
                    UPDATE su_calendar_days
                       SET status=%s, is_public=%s, synced_busy=1
                     WHERE day_date=%s
                """, ("busy", 1, d))
            else:
                cur.execute("""
                    INSERT INTO su_calendar_days (day_date, status, label, is_public, synced_busy)
                    VALUES (%s,%s,%s,%s,%s)
                """, (d, "busy", None, 1, 1))

        # 欠落日の free 化（手動 busy は保持したいので synced_busy=1 のみ対象）
        cleared = 0
        if clear_missing:
            d = start_d
            while d <= end_d:
                k = d.strftime(fmt)
                if k not in busy_days:
                    if clear_labels:
                        cur.execute("""
                            UPDATE su_calendar_days
                               SET status='free', label=NULL, synced_busy=0
                             WHERE day_date=%s AND synced_busy=1
                        """, (d,))
                    else:
                        cur.execute("""
                            UPDATE su_calendar_days
                               SET status='free', synced_busy=0
                             WHERE day_date=%s AND synced_busy=1
                        """, (d,))
                    cleared += (cur.rowcount or 0)
                d += timedelta(days=1)

        # ----- 最終同期メタ更新（テーブル自動作成） ---------------------------
        cur.execute("""
            CREATE TABLE IF NOT EXISTS su_calendar_meta (
              k VARCHAR(64) PRIMARY KEY,
              v TEXT,
              updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        # JSTの "YYYY-MM-DD HH:MM:SS+0900" 形式で保存
        last_sync_at_str = now_jst.strftime("%Y-%m-%d %H:%M:%S%z")
        cur.execute("REPLACE INTO su_calendar_meta (k, v) VALUES (%s, %s)",
                    ("last_sync_at", last_sync_at_str))
        cur.execute("REPLACE INTO su_calendar_meta (k, v) VALUES (%s, %s)",
                    ("last_sync_range", f"{start_d.isoformat()}..{end_d.isoformat()}"))

        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({"error": "db_error", "reason": repr(e)}), 500
    finally:
        try:
            cur.close()
        except Exception:
            pass

    # ----- 管理ログ ----------------------------------------------------------
    try:
        ip_for_log = (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                      or request.remote_addr or "-")
        _log_admin(
            db, ip_for_log,
            f"API_SYNC_OUTLOOK_6M {start_d.isoformat()}..{end_d.isoformat()} "
            f"busy={len(busy_days)} clear_missing={int(clear_missing)} windows={len(windows)}"
        )
    except Exception:
        pass

    return jsonify({
        "result": "ok",
        "range": {"from": start_d.strftime(fmt), "to": end_d.strftime(fmt)},
        "windows": [{"from": a.strftime(fmt), "to": b.strftime(fmt)} for (a, b) in windows],
        "count": len(busy_days),
        "cleared": (cleared if clear_missing else 0),
        "options": {"clear_missing": clear_missing, "clear_labels": clear_labels},
        "last_sync_at": last_sync_at_str,
        "last_sync_range": f"{start_d.strftime(fmt)}..{end_d.strftime(fmt)}"
    }), 200
