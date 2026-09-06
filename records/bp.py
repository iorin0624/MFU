from __future__ import annotations

import base64
import csv
import io
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps
from threading import Lock
from urllib.parse import urlencode
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, send_file, session, url_for

from app.utils.db import get_db
from app.freee_api import services as freee_services

from .models import (
    ensure_records_schema,
    get_current_odometer_km,
    insert_maintenance_item,
    list_maintenance_items,
    now_ts,
    set_current_odometer_km,
    update_maintenance_item,
)
from .uber_browser import UberAuthenticationRequired, UberPage, open_uber_login_tab, uber_browser_lock
from .uber_parser import uber_work_date
from .uber_repository import (
    activity_range_summary,
    create_import_job,
    get_active_import_job,
    get_continuous_fetch_state,
    get_import_job,
    list_activity_daily_summaries,
    list_activities,
    list_activities_for_export,
    list_import_jobs,
    update_continuous_fetch_state,
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
FREEE_AUTHORIZE_URL = "https://accounts.secure.freee.co.jp/public_api/authorize"
FREEE_TOKEN_URL = "https://accounts.secure.freee.co.jp/public_api/token"
FREEE_API_BASE_URL = "https://api.freee.co.jp"


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


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not _is_admin_user():
            return jsonify({"ok": False, "message": "管理者のみ操作できます。"}), 403
        return view(*args, **kwargs)

    return wrapper


def _uber_csrf_token() -> str:
    value = session.get("uber_browser_csrf")
    if not value:
        value = secrets.token_urlsafe(32)
        session["uber_browser_csrf"] = value
    return value


def _require_uber_csrf() -> None:
    supplied = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf_token", "")
    expected = session.get("uber_browser_csrf", "")
    if not supplied or not expected or not secrets.compare_digest(supplied, expected):
        abort(400, "CSRFトークンが一致しません。")


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


def _present_uber_activity_summary(row: dict) -> dict:
    value = dict(row or {})
    deliveries = int(value.get("deliveries") or 0)
    total_yen = sum(int(value.get(key) or 0) for key in ("net_yen", "promo_yen", "other_yen", "tip_yen"))
    duration_seconds = int(value.get("duration_seconds") or 0)
    distance_km = float(value.get("distance_km") or 0)
    value.update({
        "deliveries": deliveries,
        "total_yen": total_yen,
        "duration_hours": duration_seconds / 3600 if duration_seconds else 0,
        "deliveries_per_hour": round(deliveries * 3600 / duration_seconds, 1) if duration_seconds else None,
        "yen_per_delivery": round(total_yen / deliveries) if deliveries else None,
        "yen_per_hour": round(total_yen * 3600 / duration_seconds) if duration_seconds else None,
        "yen_per_km": round(total_yen / distance_km) if distance_km else None,
        "net_yen_per_delivery": round(int(value.get("net_yen") or 0) / deliveries) if deliveries else None,
        "net_yen_per_hour": round(int(value.get("net_yen") or 0) * 3600 / duration_seconds) if duration_seconds else None,
        "net_yen_per_km": round(int(value.get("net_yen") or 0) / distance_km) if distance_km else None,
    })
    return value


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


def _require_admin_json():
    if not _is_admin_user():
        return jsonify({"ok": False, "message": "管理者のみ操作できます。"}), 403
    return None


def _freee_redirect_uri() -> str:
    return os.getenv("FREEE_REDIRECT_URI") or url_for("records.uber_freee_callback", _external=True)


def _freee_oauth_config() -> dict:
    return {
        "client_id": os.getenv("FREEE_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("FREEE_CLIENT_SECRET", "").strip(),
        "redirect_uri": _freee_redirect_uri(),
    }


def _sanitize_freee_error(message: str) -> str:
    cleaned = str(message or "")
    for token_key in ("access_token", "refresh_token"):
        cleaned = re.sub(rf'("{token_key}"\s*:\s*")[^"]+(")', rf'\1***\2', cleaned, flags=re.I)
        cleaned = re.sub(rf"({token_key}=)[^&\s]+", rf"\1***", cleaned, flags=re.I)
    return cleaned[:2000]


def _freee_error_from_response(resp) -> str:
    body = ""
    try:
        body = json.dumps(resp.json(), ensure_ascii=False)
    except Exception:
        body = resp.text or ""
    hint = ""
    if resp.status_code == 403:
        hint = " 権限または事業所設定を確認してください。"
    elif resp.status_code == 429:
        hint = " freee API側のアクセス制限の可能性があります。時間をおいて再実行してください。"
    return _sanitize_freee_error(f"freee API error: HTTP {resp.status_code} {body}{hint}")


def _parse_optional_int_value(value):
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "":
        return None
    return int(raw)


def _get_freee_settings(db=None) -> dict | None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, company_id, partner_id, partner_code, account_item_id,
               tax_code, walletable_type, walletable_id, deal_payment_mode,
               created_at, updated_at
        FROM freee_accounting_settings
        ORDER BY id ASC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if close_db:
        db.close()
    return row


def _upsert_freee_settings(
    *,
    company_id,
    partner_id,
    partner_code,
    account_item_id,
    tax_code,
    walletable_type,
    walletable_id,
    deal_payment_mode,
) -> None:
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT id FROM freee_accounting_settings ORDER BY id ASC LIMIT 1")
    existing = cur.fetchone()
    now = now_ts()
    if existing:
        cur.execute(
            """
            UPDATE freee_accounting_settings
            SET company_id = %s,
                partner_id = %s,
                partner_code = %s,
                account_item_id = %s,
                tax_code = %s,
                walletable_type = %s,
                walletable_id = %s,
                deal_payment_mode = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (company_id, partner_id, partner_code, account_item_id, tax_code, walletable_type, walletable_id, deal_payment_mode, now, existing["id"]),
        )
    else:
        cur.execute(
            """
            INSERT INTO freee_accounting_settings (
                company_id, partner_id, partner_code, account_item_id, tax_code,
                walletable_type, walletable_id, deal_payment_mode, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (company_id, partner_id, partner_code, account_item_id, tax_code, walletable_type, walletable_id, deal_payment_mode, now, now),
        )
    db.commit()
    db.close()


def _validate_freee_settings(settings: dict | None) -> str | None:
    if not settings:
        return "freee連携設定が未完了です。company_id / account_item_id / tax_code / 決済口座を確認してください。"
    if not settings.get("company_id") or not settings.get("account_item_id") or settings.get("tax_code") is None:
        return "freee連携設定が未完了です。company_id / account_item_id / tax_code / 決済口座を確認してください。"
    mode = settings.get("deal_payment_mode") or "settled"
    if mode not in ("settled", "unsettled"):
        return "freee連携設定が未完了です。決済状態の設定を確認してください。"
    if mode == "settled" and (not settings.get("walletable_type") or not settings.get("walletable_id")):
        return "freee連携設定が未完了です。company_id / account_item_id / tax_code / 決済口座を確認してください。"
    return None


def _load_freee_token_row(db=None) -> dict | None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, access_token, refresh_token, expires_at, created_at, updated_at
        FROM freee_oauth_tokens
        WHERE provider = 'freee'
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if close_db:
        db.close()
    return row


def _save_freee_tokens(token_response: dict) -> None:
    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    if not access_token or not refresh_token:
        raise RuntimeError("freeeのトークン応答が不完全です。")
    expires_in = int(token_response.get("expires_in") or 0)
    now = now_ts()
    expires_at = now + timedelta(seconds=expires_in) if expires_in > 0 else None
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO freee_oauth_tokens (
            provider, access_token, refresh_token, expires_at, created_at, updated_at
        ) VALUES ('freee', %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            access_token = VALUES(access_token),
            refresh_token = VALUES(refresh_token),
            expires_at = VALUES(expires_at),
            updated_at = VALUES(updated_at)
        """,
        (access_token, refresh_token, expires_at, now, now),
    )
    db.commit()
    db.close()


def _refresh_freee_access_token(refresh_token: str) -> str:
    config = _freee_oauth_config()
    if not config["client_id"] or not config["client_secret"]:
        raise RuntimeError("FREEE_CLIENT_ID / FREEE_CLIENT_SECRET が未設定です。")
    resp = requests.post(
        FREEE_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(_freee_error_from_response(resp))
    token_data = resp.json()
    _save_freee_tokens(token_data)
    return token_data["access_token"]


def _get_valid_freee_access_token() -> str:
    token_row = _load_freee_token_row()
    if not token_row:
        raise RuntimeError("freeeに接続されていません。先にfreee接続を行ってください。")
    expires_at = token_row.get("expires_at")
    if expires_at and expires_at > now_ts() + timedelta(minutes=5):
        return token_row["access_token"]
    return _refresh_freee_access_token(token_row["refresh_token"])


def _freee_api_request(method: str, path: str, *, params=None, json_body=None) -> dict:
    if not path.startswith("/api/1/"):
        raise ValueError("freee API path must start with /api/1/")

    def do_request(access_token: str):
        return requests.request(
            method.upper(),
            FREEE_API_BASE_URL + path,
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=30,
        )

    access_token = _get_valid_freee_access_token()
    resp = do_request(access_token)
    if resp.status_code == 401:
        token_row = _load_freee_token_row()
        if token_row:
            access_token = _refresh_freee_access_token(token_row["refresh_token"])
            resp = do_request(access_token)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(_freee_error_from_response(resp))
    return resp.json() if resp.text else {}


def _freee_list_from_response(data: dict, key: str) -> list[dict]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def _freee_first_list(data: dict, keys: tuple[str, ...]) -> list[dict]:
    for key in keys:
        rows = _freee_list_from_response(data, key)
        if rows:
            return rows
    return []


def _freee_int_or_none(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _freee_tax_code(tax: dict) -> int | None:
    return _freee_int_or_none(tax.get("code") if tax.get("code") is not None else tax.get("tax_code"))


def _freee_tax_name(tax: dict) -> str:
    return str(tax.get("name_ja") or tax.get("name") or tax.get("display_name") or "").strip()


def _freee_walletable_id(walletable: dict) -> int | None:
    return _freee_int_or_none(walletable.get("id") if walletable.get("id") is not None else walletable.get("walletable_id"))


def _freee_walletable_type(walletable: dict) -> str:
    return str(walletable.get("walletable_type") or walletable.get("type") or "").strip()


def _format_freee_company_label(company: dict) -> str:
    name = company.get("display_name") or company.get("name") or "名称未設定"
    company_id = company.get("id") or "-"
    company_number = company.get("company_number") or "-"
    return f"{name}（ID:{company_id} / 会社番号:{company_number}）"


def _format_freee_account_item_label(item: dict) -> str:
    name = item.get("name") or item.get("display_name") or "名称未設定"
    item_id = item.get("id") or "-"
    default_tax_code = item.get("default_tax_code") or "-"
    return f"{name}（ID:{item_id} / default_tax_code:{default_tax_code}）"


def _format_freee_tax_label(tax: dict) -> str:
    name = _freee_tax_name(tax) or "名称未設定"
    code = _freee_tax_code(tax)
    return f"{name}（code:{code if code is not None else '-'}）"


def _format_freee_walletable_label(walletable: dict) -> str:
    name = walletable.get("name") or walletable.get("display_name") or "名称未設定"
    walletable_type = _freee_walletable_type(walletable) or "-"
    walletable_id = _freee_walletable_id(walletable) or "-"
    return f"{name}（{walletable_type} / ID:{walletable_id}）"


def _format_freee_partner_label(partner: dict) -> str:
    name = partner.get("name") or partner.get("display_name") or "名称未設定"
    partner_id = partner.get("id") or "-"
    return f"{name}（ID:{partner_id}）"


def _find_company_by_id(companies: list[dict], company_id: int) -> dict | None:
    for company in companies:
        if _freee_int_or_none(company.get("id")) == company_id:
            return company
    return None


def _fetch_freee_taxes(company_id: int, warnings: list[str]) -> list[dict]:
    try:
        data = _freee_api_request("GET", f"/api/1/taxes/companies/{company_id}")
        taxes = _freee_first_list(data, ("taxes", "tax_codes", "codes"))
        if taxes:
            return taxes
    except Exception as exc:
        warnings.append(
            f"税区分取得で /api/1/taxes/companies/{company_id} に失敗したため /api/1/taxes/codes にフォールバックしました。{_sanitize_freee_error(str(exc))}"
        )
    data = _freee_api_request("GET", "/api/1/taxes/codes", params={"company_id": company_id})
    return _freee_first_list(data, ("taxes", "tax_codes", "codes"))


def _fetch_freee_master_bundle(company_id: int | None = None) -> dict:
    warnings: list[str] = []
    companies = _freee_list_from_response(_freee_api_request("GET", "/api/1/companies"), "companies")
    settings = _get_freee_settings()
    selected_company_id = company_id
    if selected_company_id is None and settings and settings.get("company_id"):
        configured_id = _freee_int_or_none(settings.get("company_id"))
        if configured_id and _find_company_by_id(companies, configured_id):
            selected_company_id = configured_id
    if selected_company_id is None and len(companies) == 1:
        selected_company_id = _freee_int_or_none(companies[0].get("id"))
    if selected_company_id and not _find_company_by_id(companies, selected_company_id):
        warnings.append("選択された事業所IDはfreeeの事業所一覧に見つかりませんでした。会社番号ではなくfreee内部IDを選んでください。")

    master = {
        "companies": companies,
        "selected_company_id": selected_company_id,
        "account_items": [],
        "partners": [],
        "walletables": [],
        "taxes": [],
        "warnings": warnings,
    }
    if not selected_company_id:
        return master

    params = {"company_id": selected_company_id}
    for key, path in (
        ("account_items", "/api/1/account_items"),
        ("partners", "/api/1/partners"),
        ("walletables", "/api/1/walletables"),
    ):
        try:
            master[key] = _freee_list_from_response(_freee_api_request("GET", path, params=params), key)
        except Exception as exc:
            warnings.append(f"{key} の取得に失敗しました: {_sanitize_freee_error(str(exc))}")
    try:
        master["taxes"] = _fetch_freee_taxes(int(selected_company_id), warnings)
    except Exception as exc:
        warnings.append(f"税区分の取得に失敗しました: {_sanitize_freee_error(str(exc))}")
    return master


def _pick_account_item(account_items: list[dict]) -> dict | None:
    for item in account_items:
        if str(item.get("name") or "") == "売上高":
            return item
    for item in account_items:
        if "売上" in str(item.get("name") or ""):
            return item
    return None


def _pick_tax(taxes: list[dict], preferred_tax_code: int | None = None) -> dict | None:
    if preferred_tax_code is not None:
        for tax in taxes:
            if _freee_tax_code(tax) == preferred_tax_code:
                return tax
    for tax in taxes:
        if str(tax.get("name_ja") or "") == "課税売上10%":
            return tax
    for tax in taxes:
        if str(tax.get("name") or "") == "課税売上10%":
            return tax
    for tax in taxes:
        if _freee_tax_code(tax) == 129:
            return tax
    return None


def _pick_walletable(walletables: list[dict]) -> dict | None:
    for walletable in walletables:
        if str(walletable.get("name") or "") == "現金":
            return walletable
    for walletable in walletables:
        if "現金" in str(walletable.get("name") or ""):
            return walletable
    for walletable in walletables:
        if _freee_walletable_type(walletable) == "wallet" and "現金" in str(walletable.get("name") or ""):
            return walletable
    return None


def _pick_partner(partners: list[dict]) -> dict | None:
    for partner in partners:
        if str(partner.get("name") or "") == "Uber":
            return partner
    for partner in partners:
        name = str(partner.get("name") or "")
        if "Uber" in name or "ウーバー" in name:
            return partner
    return None


def _with_freee_labels(rows: list[dict], formatter) -> list[dict]:
    labeled = []
    for row in rows:
        item = dict(row)
        item["label"] = formatter(row)
        labeled.append(item)
    return labeled


def _pick_freee_auto_config(master: dict) -> dict:
    warnings = list(master.get("warnings") or [])
    selected_company_id = _freee_int_or_none(master.get("selected_company_id"))
    company = _find_company_by_id(master.get("companies") or [], selected_company_id) if selected_company_id else None
    if not company:
        warnings.append("freee事業所を選択してください。")

    account_item = _pick_account_item(master.get("account_items") or [])
    preferred_tax_code = _freee_int_or_none((account_item or {}).get("default_tax_code"))
    tax = _pick_tax(master.get("taxes") or [], preferred_tax_code)
    walletable = _pick_walletable(master.get("walletables") or [])
    partner = _pick_partner(master.get("partners") or [])
    if not account_item:
        warnings.append("勘定科目「売上高」を自動判定できませんでした。")
    if not tax:
        warnings.append("税区分「課税売上10%」を自動判定できませんでした。")
    if not walletable:
        warnings.append("決済口座「現金」を自動判定できませんでした。")

    return {
        "company_id": _freee_int_or_none((company or {}).get("id")),
        "company_label": _format_freee_company_label(company) if company else "",
        "account_item_id": _freee_int_or_none((account_item or {}).get("id")),
        "account_item_name": (account_item or {}).get("name"),
        "tax_code": _freee_tax_code(tax or {}),
        "tax_name": _freee_tax_name(tax or {}),
        "walletable_type": _freee_walletable_type(walletable or {}),
        "walletable_id": _freee_walletable_id(walletable or {}),
        "walletable_name": (walletable or {}).get("name"),
        "partner_id": _freee_int_or_none((partner or {}).get("id")),
        "partner_name": (partner or {}).get("name"),
        "partner_code": None,
        "deal_payment_mode": "settled",
        "warnings": warnings,
    }


def _build_freee_deal_payload(row: dict, settings: dict) -> dict:
    work_date = row["work_date"]
    deliveries = int(row.get("deliveries") or 0)
    net_yen = int(row.get("net_yen") or 0)
    promo_yen = int(row.get("promo_yen") or 0)
    other_yen = int(row.get("other_yen") or 0)
    tip_yen = int(row.get("tip_yen") or 0)
    total_yen = int(row.get("total_yen") or 0)
    description = (
        f"Uber日次売上（{deliveries}件 / "
        f"net:{net_yen} promo:{promo_yen} other:{other_yen} tip:{tip_yen}）"
    )
    payload = {
        "company_id": int(settings["company_id"]),
        "issue_date": work_date.isoformat(),
        "due_date": work_date.isoformat(),
        "type": "income",
        "ref_number": f"uber-{work_date.strftime('%Y%m%d')}",
        "details": [
            {
                "account_item_id": int(settings["account_item_id"]),
                "tax_code": int(settings["tax_code"]),
                "amount": total_yen,
                "description": description,
            }
        ],
    }
    if settings.get("partner_id"):
        payload["partner_id"] = int(settings["partner_id"])
    elif settings.get("partner_code"):
        payload["partner_code"] = str(settings["partner_code"])
    if (settings.get("deal_payment_mode") or "settled") == "settled":
        payload["payments"] = [
            {
                "date": work_date.isoformat(),
                "from_walletable_type": settings["walletable_type"],
                "from_walletable_id": int(settings["walletable_id"]),
                "amount": total_yen,
            }
        ]
    return payload


def _uber_row_needs_freee_resync(row: dict) -> bool:
    if not row.get("freee_deal_id"):
        return False
    synced_at = row.get("freee_api_synced_at")
    updated_at = row.get("updated_at")
    if not synced_at:
        return True
    return bool(updated_at and updated_at > synced_at)


def _is_freee_missing_deal_error(exc: Exception) -> bool:
    message = str(exc)
    return (
        "指定された取引は存在しません" in message
        or "存在しないか既に削除された取引です" in message
    )


def _find_uber_freee_deals_by_ref_number(work_date: date, company_id: int) -> list[dict]:
    ref_number = f"uber-{work_date.strftime('%Y%m%d')}"
    matches: list[dict] = []
    limit = 100
    for offset in range(0, 1000, limit):
        data = freee_services.freee_api_request(
            "GET",
            "/api/1/deals",
            params={
                "company_id": int(company_id),
                "issue_date_start": work_date.isoformat(),
                "issue_date_end": work_date.isoformat(),
                "type": "income",
                "limit": limit,
                "offset": offset,
            },
        )
        deals = data.get("deals") if isinstance(data, dict) else []
        deals = deals if isinstance(deals, list) else []
        matches.extend(
            deal for deal in deals
            if isinstance(deal, dict) and deal.get("ref_number") == ref_number
        )
        if len(deals) < limit:
            break
    return matches


def _save_uber_freee_link(row_id: int, deal_id: int) -> None:
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        UPDATE uber_daily
        SET freee_deal_id = %s,
            freee_api_synced_at = %s,
            freee_api_status = 'synced',
            freee_api_error = NULL,
            updated_at = updated_at
        WHERE id = %s
        """,
        (deal_id, now_ts(), row_id),
    )
    db.commit()
    db.close()


def _freee_deal_id(data: dict) -> int:
    deal = data.get("deal") if isinstance(data, dict) else None
    deal_id = (deal or {}).get("id") or (data.get("id") if isinstance(data, dict) else None)
    if not deal_id:
        raise RuntimeError(
            f"freee API response does not include deal id: {json.dumps(data, ensure_ascii=False)[:500]}"
        )
    return int(deal_id)


def _sync_uber_freee_payment(deal_id: int, payload: dict) -> None:
    payments = payload.get("payments") or []
    if not payments:
        return
    payment = dict(payments[0])
    payment["company_id"] = payload["company_id"]
    data = freee_services.freee_api_request(
        "GET",
        f"/api/1/deals/{deal_id}",
        params={"company_id": payload["company_id"]},
    )
    deal = data.get("deal") if isinstance(data, dict) else {}
    existing_payments = (deal or {}).get("payments") or []
    if existing_payments:
        payment_id = existing_payments[0].get("id")
        if payment_id:
            freee_services.freee_api_request(
                "PUT",
                f"/api/1/deals/{deal_id}/payments/{payment_id}",
                json_body=payment,
            )
            return
    freee_services.freee_api_request(
        "POST",
        f"/api/1/deals/{deal_id}/payments",
        json_body=payment,
    )


def _recover_missing_uber_freee_deal(row: dict, settings: dict, payload: dict) -> dict:
    work_date = row["work_date"]
    matches = _find_uber_freee_deals_by_ref_number(work_date, int(settings["company_id"]))
    if len(matches) > 1:
        raise RuntimeError(
            f"freeeに取引番号 uber-{work_date.strftime('%Y%m%d')} の取引が複数あります。"
            "重複登録を防ぐため自動復旧を停止しました。"
        )

    if matches:
        deal_id = int(matches[0]["id"])
        deal_payload = dict(payload)
        deal_payload.pop("payments", None)
        freee_services.freee_api_request("PUT", f"/api/1/deals/{deal_id}", json_body=deal_payload)
        if (settings.get("deal_payment_mode") or "settled") == "settled":
            _sync_uber_freee_payment(deal_id, payload)
        status = "updated"
        recovery = "relinked"
    else:
        data = freee_services.freee_api_request("POST", "/api/1/deals", json_body=payload)
        deal_id = _freee_deal_id(data)
        status = "synced"
        recovery = "recreated"

    _save_uber_freee_link(row["id"], deal_id)
    return {
        "date": work_date.isoformat(),
        "status": status,
        "freee_deal_id": deal_id,
        "recovery": recovery,
    }


def _verify_existing_uber_freee_deal(row: dict, settings: dict) -> dict:
    work_date = row["work_date"]
    date_label = work_date.isoformat()
    deal_id = int(row["freee_deal_id"])
    payload = _build_freee_deal_payload(row, settings)
    expected_ref_number = f"uber-{work_date.strftime('%Y%m%d')}"
    try:
        data = freee_services.freee_api_request(
            "GET",
            f"/api/1/deals/{deal_id}",
            params={"company_id": int(settings["company_id"])},
        )
        deal = data.get("deal") if isinstance(data, dict) else None
        if (
            not isinstance(deal, dict)
            or int(deal.get("id") or 0) != deal_id
            or deal.get("ref_number") != expected_ref_number
        ):
            return _recover_missing_uber_freee_deal(row, settings, payload)
        return {
            "date": date_label,
            "status": "skipped_already_synced",
            "freee_deal_id": deal_id,
        }
    except Exception as exc:
        if _is_freee_missing_deal_error(exc):
            try:
                return _recover_missing_uber_freee_deal(row, settings, payload)
            except Exception as recovery_exc:
                exc = recovery_exc
        message = freee_services.sanitize_freee_error(str(exc))
        _mark_uber_freee_error(row["id"], message)
        return {"date": date_label, "status": "error", "message": message}


def _update_uber_row_to_freee(row: dict, settings: dict) -> dict:
    work_date = row["work_date"]
    date_label = work_date.isoformat()
    deal_id = int(row["freee_deal_id"])
    payload = _build_freee_deal_payload(row, settings)
    deal_payload = dict(payload)
    deal_payload.pop("payments", None)
    try:
        freee_services.freee_api_request("PUT", f"/api/1/deals/{deal_id}", json_body=deal_payload)
        if (settings.get("deal_payment_mode") or "settled") == "settled":
            _sync_uber_freee_payment(deal_id, payload)
        db = get_db()
        cur = db.cursor()
        now = now_ts()
        cur.execute(
            """
            UPDATE uber_daily
            SET freee_api_synced_at = %s,
                freee_api_status = 'synced',
                freee_api_error = NULL,
                updated_at = updated_at
            WHERE id = %s
            """,
            (now, row["id"]),
        )
        db.commit()
        db.close()
        return {"date": date_label, "status": "updated", "freee_deal_id": deal_id}
    except Exception as exc:
        if _is_freee_missing_deal_error(exc):
            try:
                return _recover_missing_uber_freee_deal(row, settings, payload)
            except Exception as recovery_exc:
                exc = recovery_exc
        message = freee_services.sanitize_freee_error(str(exc))
        _mark_uber_freee_error(row["id"], message)
        return {"date": date_label, "status": "error", "message": message}


def _mark_uber_freee_error(row_id: int, message: str) -> None:
    db = get_db()
    cur = db.cursor()
    now = now_ts()
    cur.execute(
        """
        UPDATE uber_daily
        SET freee_api_status = 'error',
            freee_api_error = %s,
            updated_at = %s
        WHERE id = %s
        """,
        (freee_services.sanitize_freee_error(message), now, row_id),
    )
    db.commit()
    db.close()


def _sync_uber_row_to_freee(row: dict, settings: dict) -> dict:
    work_date = row["work_date"]
    date_label = work_date.isoformat()
    if row.get("freee_deal_id"):
        if _uber_row_needs_freee_resync(row):
            return _update_uber_row_to_freee(row, settings)
        return _verify_existing_uber_freee_deal(row, settings)
    if int(row.get("total_yen") or 0) <= 0:
        return {"date": date_label, "status": "skipped_zero_amount"}
    try:
        data = freee_services.freee_api_request("POST", "/api/1/deals", json_body=_build_freee_deal_payload(row, settings))
        freee_deal_id = _freee_deal_id(data)
        db = get_db()
        cur = db.cursor()
        now = now_ts()
        cur.execute(
            """
            UPDATE uber_daily
            SET freee_deal_id = %s,
                freee_api_synced_at = %s,
                freee_api_status = 'synced',
                freee_api_error = NULL,
                updated_at = %s
            WHERE id = %s
            """,
            (freee_deal_id, now, now, row["id"]),
        )
        db.commit()
        db.close()
        return {"date": date_label, "status": "synced", "freee_deal_id": int(freee_deal_id)}
    except Exception as exc:
        message = freee_services.sanitize_freee_error(str(exc))
        _mark_uber_freee_error(row["id"], message)
        return {"date": date_label, "status": "error", "message": message}


@records_bp.get("/")
@login_required
def index():
    # Template collision avoidance: always use the records/ namespace.
    return render_template("records/index.html")


@records_bp.get("/uber")
@login_required
def uber_list():
    rows = _fetch_uber_daily_rows(order_desc=True)
    freee_token_row = freee_services.load_freee_token_row() if _is_admin_user() else None
    freee_settings = freee_services.get_freee_deal_settings(freee_services.UBER_INTEGRATION_KEY) if _is_admin_user() else None
    freee_settings_error = freee_services.validate_freee_deal_settings(freee_settings) if _is_admin_user() else None
    db = get_db()
    cur = db.cursor(dictionary=True)

    cur.execute("SELECT COUNT(*) AS pending_count FROM uber_ocr_queue WHERE status = 'pending'")
    queue_row = cur.fetchone() or {}
    ocr_queue_pending_count = int(queue_row.get("pending_count") or 0)

    today = uber_work_date(datetime.now(ZoneInfo("Asia/Tokyo")))
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
        net_yen = row.get("net_yen") or 0
        total_yen = row.get("total_yen") or 0
        row["net_avg_yen"] = round(net_yen / deliveries) if deliveries else None
        row["total_avg_yen"] = round(total_yen / deliveries) if deliveries else None
        row["freee_needs_resync"] = bool(
            row.get("freee_deal_id")
            and row.get("freee_api_synced_at")
            and row.get("updated_at")
            and row["updated_at"] > row["freee_api_synced_at"]
        )
        row["freee_requires_sync"] = bool(
            int(row.get("total_yen") or 0) != 0
            and (
                not row.get("freee_deal_id")
                or row.get("freee_api_status") != "synced"
                or row.get("freee_needs_resync")
            )
        )
    freee_synced_date_strings = [
        str(row["work_date"])
        for row in rows
        if row.get("freee_deal_id") and not row.get("freee_needs_resync")
    ]
    sales_year_options = sorted({int(row["work_date"].year) for row in rows}, reverse=True)
    try:
        requested_sales_year = int(request.args.get("sales_year") or today.year)
    except (TypeError, ValueError):
        requested_sales_year = today.year
    selected_sales_year = (
        requested_sales_year
        if requested_sales_year in sales_year_options
        else (sales_year_options[0] if sales_year_options else today.year)
    )

    calc_basis = {
        "year": today.year,
        "month": today.month,
        "net_median": None,
        "total_median": None,
    }
    for monthly_row in monthly_rows:
        if (monthly_row.get("year") == today.year) and (monthly_row.get("month") == today.month):
            calc_basis = {
                "year": today.year,
                "month": today.month,
                "net_median": monthly_row.get("net_median"),
                "total_median": monthly_row.get("total_median"),
            }
            break

    estimate_month_options = []
    for monthly_row in monthly_rows:
        year = int(monthly_row.get("year"))
        month = int(monthly_row.get("month"))
        key = f"{year}-{month:02d}"
        estimate_month_options.append(
            {
                "key": key,
                "label": key,
                "year": year,
                "month": month,
                "net_median": monthly_row.get("net_median"),
                "total_median": monthly_row.get("total_median"),
            }
        )

    current_month_key = f"{today.year}-{today.month:02d}"
    estimate_month_keys = {option["key"] for option in estimate_month_options}
    if current_month_key in estimate_month_keys:
        default_estimate_month_key = current_month_key
    elif estimate_month_options:
        default_estimate_month_key = estimate_month_options[0]["key"]
    else:
        default_estimate_month_key = current_month_key

    deliveries_sum = summary.get("deliveries_sum") or 0
    total_sum = summary.get("total_sum") or 0
    summary_avg = round(total_sum / deliveries_sum) if deliveries_sum else None
    history_from = today - timedelta(days=30)
    history_to = today
    try:
        if request.args.get("activity_from"):
            history_from = datetime.strptime(request.args["activity_from"], "%Y-%m-%d").date()
        if request.args.get("activity_to"):
            history_to = datetime.strptime(request.args["activity_to"], "%Y-%m-%d").date()
    except ValueError:
        history_from, history_to = today - timedelta(days=30), today
    if history_from > history_to:
        history_from, history_to = history_to, history_from

    summary_mode = str(request.args.get("summary_mode") or "today")
    if summary_mode == "yesterday":
        activity_summary_from = today - timedelta(days=1)
        activity_summary_to = activity_summary_from
        activity_summary_title = "昨日の集計"
    elif summary_mode == "range":
        try:
            activity_summary_from = datetime.strptime(
                request.args.get("summary_from") or today.isoformat(), "%Y-%m-%d"
            ).date()
            activity_summary_to = datetime.strptime(
                request.args.get("summary_to") or today.isoformat(), "%Y-%m-%d"
            ).date()
        except ValueError:
            activity_summary_from = today
            activity_summary_to = today
        if activity_summary_from > activity_summary_to:
            activity_summary_from, activity_summary_to = activity_summary_to, activity_summary_from
        activity_summary_title = "期間指定の集計"
    else:
        summary_mode = "today"
        activity_summary_from = today
        activity_summary_to = today
        activity_summary_title = "本日途中集計"

    activity_daily_rows = [
        _present_uber_activity_summary(row)
        for row in list_activity_daily_summaries(history_from, history_to)
    ]

    return render_template(
        "records/uber/list.html",
        rows=rows,
        freee_synced_date_strings=freee_synced_date_strings,
        monthly_rows=monthly_rows,
        calc_basis=calc_basis,
        estimate_month_options=estimate_month_options,
        default_estimate_month_key=default_estimate_month_key,
        ocr_queue_pending_count=ocr_queue_pending_count,
        is_admin_user=_is_admin_user(),
        freee_connected=bool(freee_token_row),
        freee_settings=freee_settings,
        freee_settings_complete=bool(freee_settings and not freee_settings_error),
        freee_settings_error=freee_settings_error,
        default_work_date=today,
        default_uber_week_start=today - timedelta(days=today.weekday()),
        uber_browser_csrf=_uber_csrf_token(),
        selected_activity_summary=_present_uber_activity_summary(
            activity_range_summary(activity_summary_from, activity_summary_to)
        ),
        activity_summary_mode=summary_mode,
        activity_summary_from=activity_summary_from,
        activity_summary_to=activity_summary_to,
        activity_summary_title=activity_summary_title,
        activity_daily_rows=activity_daily_rows,
        uber_activity_rows=list_activities(history_from, history_to),
        activity_history_from=history_from,
        activity_history_to=history_to,
        uber_import_jobs=list_import_jobs(10),
        active_uber_import_job=get_active_import_job(),
        uber_continuous_state=get_continuous_fetch_state(),
        sales_year_options=sales_year_options,
        selected_sales_year=selected_sales_year,
        summary={
            "deliveries_sum": deliveries_sum,
            "net_sum": summary.get("net_sum") or 0,
            "total_sum": total_sum,
            "avg_yen": summary_avg,
            "month_start": month_start,
        },
    )


@records_bp.post("/uber/browser/start")
@login_required
@admin_required
def uber_browser_start():
    _require_uber_csrf()
    try:
        with uber_browser_lock():
            result = open_uber_login_tab()
        return jsonify({"ok": True, **result})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@records_bp.get("/uber/browser/status")
@login_required
@admin_required
def uber_browser_status():
    try:
        with uber_browser_lock(blocking=False), UberPage() as page:
            page.ensure_logged_in()
        return jsonify({"ok": True, "loggedIn": True})
    except UberAuthenticationRequired as exc:
        return jsonify({"ok": True, "loggedIn": False, "message": str(exc)})
    except RuntimeError as exc:
        return jsonify({"ok": True, "loggedIn": False, "message": str(exc)})
    except Exception as exc:
        return jsonify({"ok": False, "loggedIn": False, "message": str(exc)}), 500


@records_bp.post("/uber/import-jobs")
@login_required
@admin_required
def uber_import_job_create():
    _require_uber_csrf()
    payload = request.get_json(silent=True) or request.form
    try:
        date_from = datetime.strptime(str(payload.get("date_from") or ""), "%Y-%m-%d").date()
        date_to = datetime.strptime(str(payload.get("date_to") or ""), "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"ok": False, "message": "取得日を正しく指定してください。"}), 400
    today = uber_work_date(datetime.now(ZoneInfo("Asia/Tokyo")))
    if date_from > date_to:
        return jsonify({"ok": False, "message": "開始日は終了日以前にしてください。"}), 400
    if date_to > today:
        return jsonify({"ok": False, "message": "未来の日付は取得できません。"}), 400
    if date_from < date(2015, 1, 1) or (date_to - date_from).days > 3650:
        return jsonify({"ok": False, "message": "一度に指定できる期間は2015年以降の10年間までです。"}), 400
    active = get_active_import_job()
    if active:
        return jsonify({"ok": False, "message": "別のUber取得処理が実行中です。", "jobId": active["id"]}), 409
    try:
        job_id = create_import_job(date_from, date_to)
    except RuntimeError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 409
    root = str(Path(current_app.root_path).parent)
    try:
        subprocess.Popen(
            [
                sys.executable, "-m", "app.records.uber_fetch_cli", "--job-id", job_id,
                "--date-from", date_from.isoformat(), "--date-to", date_to.isoformat(),
            ],
            cwd=root,
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as exc:
        from .uber_repository import update_import_job

        update_import_job(job_id, status="error", error=str(exc), finished_at=datetime.now())
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, "jobId": job_id, "status": "pending"}), 202


@records_bp.get("/uber/import-jobs/<job_id>")
@login_required
@admin_required
def uber_import_job_status(job_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        abort(404)
    job = get_import_job(job_id)
    if not job:
        abort(404)
    total = int(job.get("total_days") or 0)
    processed = int(job.get("processed_days") or 0)
    job["progress"] = round(processed * 100 / total) if total else 0
    return jsonify({"ok": True, "job": job})


def _start_uber_continuous_process(*, force: bool = False) -> None:
    root = str(Path(current_app.root_path).parent)
    command = [sys.executable, "-m", "app.records.uber_continuous_fetch_cli"]
    if force:
        command.append("--force")
    subprocess.Popen(
        command,
        cwd=root,
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )


@records_bp.get("/uber/continuous-fetch")
@login_required
@admin_required
def uber_continuous_fetch_status():
    return jsonify({"ok": True, "state": get_continuous_fetch_state()})


@records_bp.post("/uber/continuous-fetch/start")
@login_required
@admin_required
def uber_continuous_fetch_start():
    _require_uber_csrf()
    active = get_active_import_job()
    if active:
        return jsonify({"ok": False, "message": "別のUber取得処理が実行中です。"}), 409
    now = datetime.now(ZoneInfo("Asia/Tokyo")).replace(tzinfo=None)
    work_date = uber_work_date(datetime.now(ZoneInfo("Asia/Tokyo")))
    state = update_continuous_fetch_state(
        enabled=1,
        active_work_date=work_date,
        status="monitoring",
        started_at=now,
        stopped_at=None,
        next_run_at=now,
        consecutive_errors=0,
        last_error=None,
    )
    try:
        _start_uber_continuous_process(force=True)
    except Exception as exc:
        update_continuous_fetch_state(enabled=0, status="error_paused", stopped_at=now, last_error=str(exc))
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, "state": state, "message": "継続取得を開始しました。"}), 202


@records_bp.post("/uber/continuous-fetch/stop")
@login_required
@admin_required
def uber_continuous_fetch_stop():
    _require_uber_csrf()
    now = datetime.now(ZoneInfo("Asia/Tokyo")).replace(tzinfo=None)
    state = update_continuous_fetch_state(
        enabled=0,
        status="stopped",
        stopped_at=now,
        next_run_at=None,
    )
    return jsonify({"ok": True, "state": state, "message": "継続取得を停止しました。"})


@records_bp.post("/uber/continuous-fetch/run-now")
@login_required
@admin_required
def uber_continuous_fetch_run_now():
    _require_uber_csrf()
    state = get_continuous_fetch_state()
    if not state.get("enabled"):
        return jsonify({"ok": False, "message": "先に継続取得を開始してください。"}), 409
    if get_active_import_job():
        return jsonify({"ok": False, "message": "別のUber取得処理が実行中です。"}), 409
    try:
        _start_uber_continuous_process(force=True)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500
    return jsonify({"ok": True, "message": "増分取得を開始しました。"}), 202


def _fetch_uber_daily_rows(
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    dates: list[date] | None = None,
    order_desc: bool = True,
) -> list[dict]:
    db = get_db()
    cur = db.cursor(dictionary=True)
    where_clauses: list[str] = []
    params: list[date] = []
    if from_date is not None:
        where_clauses.append("work_date >= %s")
        params.append(from_date)
    if to_date is not None:
        where_clauses.append("work_date <= %s")
        params.append(to_date)
    if dates:
        placeholders = ", ".join(["%s"] * len(dates))
        where_clauses.append(f"work_date IN ({placeholders})")
        params.extend(dates)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    order_sql = "DESC" if order_desc else "ASC"
    cur.execute(
        f"""
        SELECT
            id,
            work_date,
            deliveries,
            net_yen,
            promo_yen,
            other_yen,
            tip_yen,
            created_at,
            updated_at,
            freee_exported_at,
            freee_deal_id,
            freee_api_synced_at,
            freee_api_status,
            freee_api_error,
            source,
            (net_yen + promo_yen + other_yen + tip_yen) AS total_yen
        FROM uber_daily
        {where_sql}
        ORDER BY work_date {order_sql}
        """,
        tuple(params),
    )
    rows = cur.fetchall()
    db.close()
    return rows


def _build_uber_freee_csv_response(rows: list[dict]) -> Response:
    export_rows = [row for row in rows if int(row.get("total_yen") or 0) > 0]
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(
        [
            "収支区分",
            "管理番号",
            "発生日",
            "決済期日",
            "取引先",
            "勘定科目",
            "税区分",
            "金額",
            "税計算区分",
            "税額",
            "備考",
            "品目",
            "部門",
            "メモタグ",
            "セグメント1",
            "セグメント2",
            "セグメント3",
            "決済日",
            "決済口座",
            "決済金額",
        ]
    )

    for row in export_rows:
        work_date = row["work_date"]
        deliveries = int(row.get("deliveries") or 0)
        net_yen = int(row.get("net_yen") or 0)
        promo_yen = int(row.get("promo_yen") or 0)
        other_yen = int(row.get("other_yen") or 0)
        tip_yen = int(row.get("tip_yen") or 0)
        total_yen = int(row.get("total_yen") or 0)
        writer.writerow(
            [
                "収入",
                f"uber-{work_date.strftime('%Y%m%d')}",
                work_date.strftime("%Y/%m/%d"),
                "",
                "Uber",
                "売上高",
                "課税売上10%",
                str(total_yen),
                "内税",
                "",
                f"Uber日次売上（{deliveries}件 / net:{net_yen} promo:{promo_yen} other:{other_yen} tip:{tip_yen}）",
                "",
                "",
                "",
                "",
                "",
                "",
                work_date.strftime("%Y/%m/%d"),
                "現金",
                str(total_yen),
            ]
        )

    csv_bytes = output.getvalue().encode("utf-8-sig")
    output.close()
    if export_rows:
        from_label = export_rows[0]["work_date"].strftime("%Y%m%d")
        to_label = export_rows[-1]["work_date"].strftime("%Y%m%d")
    else:
        label_date = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d")
        from_label = label_date
        to_label = label_date
    filename = f"freee_uber_{from_label}-{to_label}.csv"
    return Response(
        csv_bytes,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_uber_daily_csv_response(rows: list[dict]) -> Response:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["日付", "件数", "正味", "プロモ", "その他", "チップ", "合計", "正味平均", "合計平均"])

    for row in rows:
        work_date = row["work_date"]
        if isinstance(work_date, str):
            date_label = work_date
        else:
            date_label = work_date.strftime("%Y-%m-%d")
        deliveries = int(row.get("deliveries") or 0)
        net_yen = int(row.get("net_yen") or 0)
        promo_yen = int(row.get("promo_yen") or 0)
        other_yen = int(row.get("other_yen") or 0)
        tip_yen = int(row.get("tip_yen") or 0)
        total_yen = int(row.get("total_yen") or 0)
        net_avg_yen = round(net_yen / deliveries) if deliveries else ""
        total_avg_yen = round(total_yen / deliveries) if deliveries else ""
        writer.writerow(
            [
                date_label,
                deliveries,
                net_yen,
                promo_yen,
                other_yen,
                tip_yen,
                total_yen,
                net_avg_yen,
                total_avg_yen,
            ]
        )

    csv_bytes = output.getvalue().encode("utf-8-sig")
    output.close()
    if rows:
        first_date = rows[0]["work_date"]
        last_date = rows[-1]["work_date"]
        if isinstance(first_date, str):
            from_label = first_date.replace("-", "")
        else:
            from_label = first_date.strftime("%Y%m%d")
        if isinstance(last_date, str):
            to_label = last_date.replace("-", "")
        else:
            to_label = last_date.strftime("%Y%m%d")
    else:
        label_date = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y%m%d")
        from_label = label_date
        to_label = label_date
    filename = f"uber_daily_{from_label}-{to_label}.csv"
    return Response(
        csv_bytes,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _format_activity_duration(value) -> str:
    if value is None:
        return ""
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _csv_safe_text(value) -> str:
    text = str(value or "")
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _build_uber_activity_csv_response(rows: list[dict], date_from: date, date_to: date) -> Response:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(["日時", "種別", "件数", "時間", "距離", "売上", "現金", "店舗", "配達先"])
    type_labels = {"delivery": "配達", "quest": "クエスト", "other": "その他"}
    for row in rows:
        occurred_at = row.get("occurred_at")
        writer.writerow(
            [
                occurred_at.strftime("%Y-%m-%d %H:%M:%S") if occurred_at else "",
                type_labels.get(str(row.get("activity_type") or ""), "その他"),
                int(row.get("deliveries") or 0),
                _format_activity_duration(row.get("duration_seconds")),
                "" if row.get("distance_km") is None else str(row["distance_km"]),
                int(row.get("earnings_yen") or 0),
                int(row.get("cash_collected_yen") or 0),
                _csv_safe_text(row.get("merchant_name")),
                _csv_safe_text(row.get("delivery_address")),
            ]
        )
    csv_bytes = output.getvalue().encode("utf-8-sig")
    output.close()
    filename = f"uber_activities_{date_from:%Y%m%d}-{date_to:%Y%m%d}.csv"
    return Response(
        csv_bytes,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@records_bp.get("/uber.csv")
@login_required
def uber_csv():
    rows = _fetch_uber_daily_rows(order_desc=False)
    return _build_uber_daily_csv_response(rows)


@records_bp.get("/uber/activities.csv")
@login_required
@admin_required
def uber_activity_csv():
    try:
        date_from = datetime.strptime(str(request.args.get("activity_from") or ""), "%Y-%m-%d").date()
        date_to = datetime.strptime(str(request.args.get("activity_to") or ""), "%Y-%m-%d").date()
    except ValueError:
        abort(400, "開始日と終了日を正しく指定してください。")
    if date_from > date_to:
        abort(400, "開始日は終了日以前にしてください。")
    if date_from < date(2015, 1, 1) or (date_to - date_from).days > 3650:
        abort(400, "CSVに指定できる期間は2015年以降の10年間までです。")
    return _build_uber_activity_csv_response(
        list_activities_for_export(date_from, date_to), date_from, date_to
    )


@records_bp.post("/uber/freee.csv")
@login_required
def uber_freee_csv():
    payload = request.get_json(silent=True) or {}
    raw_dates = payload.get("dates")
    if not isinstance(raw_dates, list):
        return jsonify({"ok": False, "message": "dates は配列で指定してください。"}), 400
    parsed_dates: list[date] = []
    seen_dates: set[date] = set()
    for value in raw_dates:
        if not isinstance(value, str):
            return jsonify({"ok": False, "message": "dates は YYYY-MM-DD 形式の配列で指定してください。"}), 400
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"ok": False, "message": "dates は YYYY-MM-DD 形式で指定してください。"}), 400
        if parsed not in seen_dates:
            seen_dates.add(parsed)
            parsed_dates.append(parsed)
    if not parsed_dates:
        return jsonify({"ok": False, "message": "少なくとも1日以上選択してください。"}), 400

    rows = _fetch_uber_daily_rows(dates=parsed_dates, order_desc=False)
    response = _build_uber_freee_csv_response(rows)

    db = get_db()
    cur = db.cursor()
    now = now_ts()
    placeholders = ", ".join(["%s"] * len(parsed_dates))
    cur.execute(
        f"""
        UPDATE uber_daily
        SET freee_exported_at = %s,
            updated_at = %s
        WHERE work_date IN ({placeholders})
        """,
        (now, now, *parsed_dates),
    )
    db.commit()
    db.close()

    return response


@records_bp.get("/uber/freee/connect")
@login_required
def uber_freee_connect():
    admin_redirect = _require_admin_for_records()
    if admin_redirect:
        return admin_redirect
    config = _freee_oauth_config()
    if not config["client_id"]:
        flash("FREEE_CLIENT_ID が未設定です。", "warning")
        return redirect(url_for("records.uber_list"))
    state = secrets.token_urlsafe(32)
    session["freee_oauth_state"] = state
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config["client_id"],
            "redirect_uri": config["redirect_uri"],
            "state": state,
            "prompt": "select_company",
        }
    )
    return redirect(f"{FREEE_AUTHORIZE_URL}?{query}")


@records_bp.get("/uber/freee/callback")
@login_required
def uber_freee_callback():
    admin_redirect = _require_admin_for_records()
    if admin_redirect:
        return admin_redirect
    if request.args.get("error"):
        flash(f"freee接続がキャンセルまたは失敗しました: {request.args.get('error')}", "warning")
        return redirect(url_for("records.uber_list"))
    expected_state = session.pop("freee_oauth_state", None)
    actual_state = request.args.get("state")
    if not expected_state or actual_state != expected_state:
        flash("freee OAuth state が一致しません。もう一度接続してください。", "danger")
        return redirect(url_for("records.uber_list"))
    code = request.args.get("code")
    if not code:
        flash("freee OAuth code が取得できませんでした。", "danger")
        return redirect(url_for("records.uber_list"))
    config = _freee_oauth_config()
    if not config["client_id"] or not config["client_secret"]:
        flash("FREEE_CLIENT_ID / FREEE_CLIENT_SECRET が未設定です。", "warning")
        return redirect(url_for("records.uber_list"))
    try:
        resp = requests.post(
            FREEE_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": config["client_id"],
                "client_secret": config["client_secret"],
                "code": code,
                "redirect_uri": config["redirect_uri"],
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(_freee_error_from_response(resp))
        _save_freee_tokens(resp.json())
        flash("freeeに接続しました。", "success")
    except Exception as exc:
        flash(_sanitize_freee_error(str(exc)), "danger")
    return redirect(url_for("records.uber_list"))


@records_bp.post("/uber/freee/settings")
@login_required
def uber_freee_save_settings():
    admin_redirect = _require_admin_for_records()
    if admin_redirect:
        return admin_redirect
    source = request.get_json(silent=True) if request.is_json else request.form
    try:
        company_id = _parse_optional_int_value(source.get("company_id"))
        partner_id = _parse_optional_int_value(source.get("partner_id"))
        partner_code = (source.get("partner_code") or "").strip() or None
        account_item_id = _parse_optional_int_value(source.get("account_item_id"))
        tax_code = _parse_optional_int_value(source.get("tax_code"))
        walletable_type = (source.get("walletable_type") or "").strip() or None
        walletable_id = _parse_optional_int_value(source.get("walletable_id"))
        walletable_value = (source.get("walletable") or "").strip()
        if walletable_value:
            raw_type, sep, raw_id = walletable_value.partition(":")
            if sep:
                walletable_type = raw_type.strip() or None
                walletable_id = _parse_optional_int_value(raw_id)
        deal_payment_mode = (source.get("deal_payment_mode") or "settled").strip()
    except (TypeError, ValueError):
        flash("freee連携設定の数値項目を確認してください。", "warning")
        return redirect(url_for("records.uber_list"))

    settings = {
        "company_id": company_id,
        "partner_id": partner_id,
        "partner_code": partner_code,
        "account_item_id": account_item_id,
        "tax_code": tax_code,
        "walletable_type": walletable_type,
        "walletable_id": walletable_id,
        "deal_payment_mode": deal_payment_mode,
    }
    if company_id and _load_freee_token_row():
        try:
            companies = _freee_list_from_response(_freee_api_request("GET", "/api/1/companies"), "companies")
            if not _find_company_by_id(companies, int(company_id)):
                matched_number = any(str(company.get("company_number") or "") == str(company_id) for company in companies)
                if matched_number:
                    flash("事業所IDには会社番号ではなく、freee内部IDを選択してください。", "warning")
                    return redirect(url_for("records.uber_list"))
        except Exception:
            pass
    error = _validate_freee_settings(settings)
    if error:
        flash(error, "warning")
        return redirect(url_for("records.uber_list"))
    _upsert_freee_settings(**settings)
    flash("freee連携設定を保存しました。", "success")
    return redirect(url_for("records.uber_list"))


@records_bp.get("/uber/freee/master-data")
@login_required
def uber_freee_master_data():
    admin_error = _require_admin_json()
    if admin_error:
        return admin_error
    settings = _get_freee_settings()
    raw_company_id = request.args.get("company_id") or (settings or {}).get("company_id")
    try:
        company_id = int(raw_company_id) if raw_company_id else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "company_id を確認してください。"}), 400
    try:
        data = {"companies": _freee_api_request("GET", "/api/1/companies").get("companies", [])}
        if company_id:
            params = {"company_id": company_id}
            data["account_items"] = _freee_api_request("GET", "/api/1/account_items", params=params).get("account_items", [])
            data["partners"] = _freee_api_request("GET", "/api/1/partners", params=params).get("partners", [])
            data["walletables"] = _freee_api_request("GET", "/api/1/walletables", params=params).get("walletables", [])
            warnings = []
            data["taxes"] = _fetch_freee_taxes(int(company_id), warnings)
            data["warnings"] = warnings
        return jsonify({"ok": True, **data})
    except Exception as exc:
        return jsonify({"ok": False, "message": _sanitize_freee_error(str(exc))}), 500


@records_bp.get("/uber/freee/auto-config-candidates")
@login_required
def uber_freee_auto_config_candidates():
    admin_error = _require_admin_json()
    if admin_error:
        return admin_error
    if not freee_services.load_freee_token_row():
        return jsonify({"ok": False, "message": "freeeに接続されていません。先にfreee接続を行ってください。"}), 401
    try:
        raw_company_id = request.args.get("company_id")
        company_id = int(raw_company_id) if raw_company_id else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "freee事業所を選択してください。"}), 400
    try:
        master = _fetch_freee_master_bundle(company_id)
        suggested = _pick_freee_auto_config(master)
        return jsonify(
            {
                "ok": True,
                "companies": _with_freee_labels(master.get("companies") or [], _format_freee_company_label),
                "account_items": _with_freee_labels(master.get("account_items") or [], _format_freee_account_item_label),
                "taxes": _with_freee_labels(master.get("taxes") or [], _format_freee_tax_label),
                "walletables": _with_freee_labels(master.get("walletables") or [], _format_freee_walletable_label),
                "partners": _with_freee_labels(master.get("partners") or [], _format_freee_partner_label),
                "selected_company_id": master.get("selected_company_id"),
                "suggested": suggested,
                "warnings": list(master.get("warnings") or []) + list(suggested.get("warnings") or []),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "message": _sanitize_freee_error(str(exc))}), 500


@records_bp.post("/uber/freee/auto-config-save")
@login_required
def uber_freee_auto_config_save():
    admin_error = _require_admin_json()
    if admin_error:
        return admin_error
    if not freee_services.load_freee_token_row():
        return jsonify({"ok": False, "message": "freeeに接続されていません。先にfreee接続を行ってください。"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "freee事業所を選択してください。"}), 400
    if company_id <= 0:
        return jsonify({"ok": False, "message": "freee事業所を選択してください。"}), 400
    try:
        master = _fetch_freee_master_bundle(company_id)
        suggested = _pick_freee_auto_config(master)
        settings = {
            "company_id": suggested.get("company_id"),
            "partner_id": suggested.get("partner_id"),
            "partner_code": suggested.get("partner_code"),
            "account_item_id": suggested.get("account_item_id"),
            "tax_code": suggested.get("tax_code"),
            "walletable_type": suggested.get("walletable_type"),
            "walletable_id": suggested.get("walletable_id"),
            "deal_payment_mode": suggested.get("deal_payment_mode") or "settled",
        }
        warnings = list(master.get("warnings") or []) + list(suggested.get("warnings") or [])
        if _validate_freee_settings(settings):
            return jsonify(
                {
                    "ok": False,
                    "message": "自動設定に必要な勘定科目または税区分または決済口座が見つかりませんでした。候補一覧から手動選択してください。",
                    "warnings": warnings,
                    "suggested": suggested,
                }
            ), 400
        _upsert_freee_settings(**settings)
        return jsonify(
            {
                "ok": True,
                "message": "freee連携設定を自動保存しました。",
                "settings": {
                    **settings,
                    "account_item_name": suggested.get("account_item_name"),
                    "tax_name": suggested.get("tax_name"),
                    "walletable_name": suggested.get("walletable_name"),
                    "partner_name": suggested.get("partner_name"),
                },
                "warnings": warnings,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "message": _sanitize_freee_error(str(exc))}), 500


@records_bp.post("/uber/freee/api-sync")
@login_required
def uber_freee_api_sync():
    admin_error = _require_admin_json()
    if admin_error:
        return admin_error
    payload = request.get_json(silent=True) or {}
    raw_dates = payload.get("dates")
    if not isinstance(raw_dates, list):
        return jsonify({"ok": False, "message": "dates は配列で指定してください。"}), 400
    parsed_dates: list[date] = []
    seen_dates: set[date] = set()
    for value in raw_dates:
        if not isinstance(value, str):
            return jsonify({"ok": False, "message": "dates は YYYY-MM-DD 形式の配列で指定してください。"}), 400
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"ok": False, "message": "dates は YYYY-MM-DD 形式で指定してください。"}), 400
        if parsed not in seen_dates:
            seen_dates.add(parsed)
            parsed_dates.append(parsed)
    if not parsed_dates:
        return jsonify({"ok": False, "message": "少なくとも1日以上選択してください。"}), 400

    settings = freee_services.get_freee_deal_settings(freee_services.UBER_INTEGRATION_KEY)
    settings_error = freee_services.validate_freee_deal_settings(settings)
    if settings_error:
        return jsonify({"ok": False, "message": settings_error}), 400
    if not freee_services.load_freee_token_row():
        return jsonify({"ok": False, "message": "freeeに接続されていません。先にfreee接続を行ってください。"}), 401

    rows = _fetch_uber_daily_rows(dates=parsed_dates, order_desc=False)
    rows_by_date = {row["work_date"]: row for row in rows}
    results = []
    for target_date in parsed_dates:
        row = rows_by_date.get(target_date)
        if not row:
            results.append({"date": target_date.isoformat(), "status": "skipped_not_found"})
            continue
        results.append(_sync_uber_row_to_freee(row, settings))

    synced = sum(1 for item in results if item.get("status") == "synced")
    updated = sum(1 for item in results if item.get("status") == "updated")
    failed = sum(1 for item in results if item.get("status") == "error")
    skipped = len(results) - synced - updated - failed
    response_body = {
        "ok": failed < len(results),
        "synced": synced,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }
    if results and failed == len(results):
        return jsonify(response_body), 500
    return jsonify(response_body)


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
