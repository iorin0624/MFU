from __future__ import annotations

import json
import os
import re
from datetime import timedelta

import requests
from flask import url_for

from app.utils.db import get_db

from .models import ensure_freee_api_schema, now_ts

FREEE_AUTHORIZE_URL = "https://accounts.secure.freee.co.jp/public_api/authorize"
FREEE_TOKEN_URL = "https://accounts.secure.freee.co.jp/public_api/token"
FREEE_API_BASE_URL = "https://api.freee.co.jp"
UBER_INTEGRATION_KEY = "uber"
INVOICE_INTEGRATION_KEY = "invoice"


def freee_redirect_uri() -> str:
    return os.getenv("FREEE_REDIRECT_URI") or url_for("freee_api.callback", _external=True)


def freee_oauth_config() -> dict:
    return {
        "client_id": os.getenv("FREEE_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("FREEE_CLIENT_SECRET", "").strip(),
        "redirect_uri": freee_redirect_uri(),
    }


def sanitize_freee_error(message: str) -> str:
    cleaned = str(message or "")
    for token_key in ("access_token", "refresh_token"):
        cleaned = re.sub(rf'("{token_key}"\s*:\s*")[^"]+(")', rf"\1***\2", cleaned, flags=re.I)
        cleaned = re.sub(rf"({token_key}=)[^&\s]+", rf"\1***", cleaned, flags=re.I)
    return cleaned[:2000]


def freee_error_from_response(resp) -> str:
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
    return sanitize_freee_error(f"freee API error: HTTP {resp.status_code} {body}{hint}")


def parse_optional_int_value(value):
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "":
        return None
    return int(raw)


def load_freee_token_row(db=None) -> dict | None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    ensure_freee_api_schema(db)
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


def save_freee_tokens(token_response: dict) -> None:
    access_token = token_response.get("access_token")
    refresh_token = token_response.get("refresh_token")
    if not access_token or not refresh_token:
        raise RuntimeError("freeeのトークン応答が不完全です。")
    expires_in = int(token_response.get("expires_in") or 0)
    now = now_ts()
    expires_at = now + timedelta(seconds=expires_in) if expires_in > 0 else None
    db = get_db()
    ensure_freee_api_schema(db)
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


def refresh_freee_access_token(refresh_token: str) -> str:
    config = freee_oauth_config()
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
        raise RuntimeError(freee_error_from_response(resp))
    token_data = resp.json()
    save_freee_tokens(token_data)
    return token_data["access_token"]


def get_valid_freee_access_token() -> str:
    token_row = load_freee_token_row()
    if not token_row:
        raise RuntimeError("freeeに接続されていません。先にfreee接続を行ってください。")
    expires_at = token_row.get("expires_at")
    if expires_at and expires_at > now_ts() + timedelta(minutes=5):
        return token_row["access_token"]
    return refresh_freee_access_token(token_row["refresh_token"])


def freee_api_request(method: str, path: str, *, params=None, json_body=None) -> dict:
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

    access_token = get_valid_freee_access_token()
    resp = do_request(access_token)
    if resp.status_code == 401:
        token_row = load_freee_token_row()
        if token_row:
            access_token = refresh_freee_access_token(token_row["refresh_token"])
            resp = do_request(access_token)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(freee_error_from_response(resp))
    return resp.json() if resp.text else {}


def get_freee_common_settings(db=None) -> dict | None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    ensure_freee_api_schema(db)
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, company_id, created_at, updated_at
        FROM freee_common_settings
        ORDER BY id ASC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if close_db:
        db.close()
    return row


def upsert_freee_common_settings(*, company_id) -> None:
    db = get_db()
    ensure_freee_api_schema(db)
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT id FROM freee_common_settings ORDER BY id ASC LIMIT 1")
    existing = cur.fetchone()
    now = now_ts()
    if existing:
        cur.execute(
            """
            UPDATE freee_common_settings
            SET company_id = %s, updated_at = %s
            WHERE id = %s
            """,
            (company_id, now, existing["id"]),
        )
    else:
        cur.execute(
            """
            INSERT INTO freee_common_settings (company_id, created_at, updated_at)
            VALUES (%s, %s, %s)
            """,
            (company_id, now, now),
        )
    db.commit()
    db.close()


def get_freee_integration_settings(integration_key: str, db=None) -> dict | None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    ensure_freee_api_schema(db)
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, integration_key, account_item_id, tax_code, tax_code_8, tax_code_nontax, deal_payment_mode,
               walletable_type, walletable_id, partner_id, partner_code,
               created_at, updated_at
        FROM freee_integration_settings
        WHERE integration_key = %s
        LIMIT 1
        """,
        (integration_key,),
    )
    row = cur.fetchone()
    if close_db:
        db.close()
    return row


def upsert_freee_integration_settings(
    *,
    integration_key: str,
    account_item_id,
    tax_code,
    tax_code_8=None,
    tax_code_nontax=None,
    deal_payment_mode="settled",
    walletable_type=None,
    walletable_id=None,
    partner_id=None,
    partner_code=None,
) -> None:
    db = get_db()
    ensure_freee_api_schema(db)
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT id FROM freee_integration_settings WHERE integration_key = %s LIMIT 1", (integration_key,))
    existing = cur.fetchone()
    now = now_ts()
    values = (
        account_item_id,
        tax_code,
        tax_code_8,
        tax_code_nontax,
        deal_payment_mode,
        walletable_type,
        walletable_id,
        partner_id,
        partner_code,
        now,
    )
    if existing:
        cur.execute(
            """
            UPDATE freee_integration_settings
            SET account_item_id = %s,
                tax_code = %s,
                tax_code_8 = %s,
                tax_code_nontax = %s,
                deal_payment_mode = %s,
                walletable_type = %s,
                walletable_id = %s,
                partner_id = %s,
                partner_code = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (*values, existing["id"]),
        )
    else:
        cur.execute(
            """
            INSERT INTO freee_integration_settings (
                integration_key, account_item_id, tax_code, tax_code_8, tax_code_nontax, deal_payment_mode,
                walletable_type, walletable_id, partner_id, partner_code,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                integration_key,
                account_item_id,
                tax_code,
                tax_code_8,
                tax_code_nontax,
                deal_payment_mode,
                walletable_type,
                walletable_id,
                partner_id,
                partner_code,
                now,
                now,
            ),
        )
    db.commit()
    db.close()


def validate_freee_common_settings(settings: dict | None) -> str | None:
    if not settings or not settings.get("company_id"):
        return "freee共通設定が未完了です。事業所を選択してください。"
    return None


def validate_freee_integration_settings(settings: dict | None) -> str | None:
    if not settings:
        return "freee用途別設定が未完了です。勘定科目 / 税区分 / 決済設定を確認してください。"
    if not settings.get("account_item_id") or settings.get("tax_code") is None:
        return "freee用途別設定が未完了です。勘定科目 / 税区分を確認してください。"
    mode = settings.get("deal_payment_mode") or "settled"
    if mode not in ("settled", "unsettled"):
        return "freee用途別設定が未完了です。決済状態の設定を確認してください。"
    if mode == "settled" and (not settings.get("walletable_type") or not settings.get("walletable_id")):
        return "freee用途別設定が未完了です。決済口座を確認してください。"
    return None


def validate_freee_invoice_settings(settings: dict | None) -> str | None:
    error = validate_freee_integration_settings(settings)
    if error:
        return error
    if settings.get("tax_code_8") is None or settings.get("tax_code_nontax") is None:
        return "freee請求書設定が未完了です。税区分コード 8% / 対象外を確認してください。"
    return None


def get_freee_deal_settings(integration_key: str) -> dict | None:
    common = get_freee_common_settings()
    integration = get_freee_integration_settings(integration_key)
    if not common and not integration:
        return None
    return {**(integration or {}), "company_id": (common or {}).get("company_id")}


def validate_freee_deal_settings(settings: dict | None) -> str | None:
    if not settings or not settings.get("company_id"):
        return "freee共通設定が未完了です。事業所を選択してください。"
    if settings.get("integration_key") == INVOICE_INTEGRATION_KEY:
        return validate_freee_invoice_settings(settings)
    return validate_freee_integration_settings(settings)


def freee_list_from_response(data: dict, key: str) -> list[dict]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def freee_first_list(data: dict, keys: tuple[str, ...]) -> list[dict]:
    for key in keys:
        rows = freee_list_from_response(data, key)
        if rows:
            return rows
    return []


def freee_int_or_none(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def freee_tax_code(tax: dict) -> int | None:
    return freee_int_or_none(tax.get("code") if tax.get("code") is not None else tax.get("tax_code"))


def freee_tax_name(tax: dict) -> str:
    return str(tax.get("name_ja") or tax.get("name") or tax.get("display_name") or "").strip()


def freee_walletable_id(walletable: dict) -> int | None:
    return freee_int_or_none(walletable.get("id") if walletable.get("id") is not None else walletable.get("walletable_id"))


def freee_walletable_type(walletable: dict) -> str:
    return str(walletable.get("walletable_type") or walletable.get("type") or "").strip()


def format_freee_company_label(company: dict) -> str:
    name = company.get("display_name") or company.get("name") or "名称未設定"
    company_id = company.get("id") or "-"
    company_number = company.get("company_number") or "-"
    return f"{name}（ID:{company_id} / 会社番号:{company_number}）"


def format_freee_account_item_label(item: dict) -> str:
    name = item.get("name") or item.get("display_name") or "名称未設定"
    item_id = item.get("id") or "-"
    default_tax_code = item.get("default_tax_code") or "-"
    return f"{name}（ID:{item_id} / default_tax_code:{default_tax_code}）"


def format_freee_tax_label(tax: dict) -> str:
    name = freee_tax_name(tax) or "名称未設定"
    code = freee_tax_code(tax)
    return f"{name}（code:{code if code is not None else '-'}）"


def format_freee_walletable_label(walletable: dict) -> str:
    name = walletable.get("name") or walletable.get("display_name") or "名称未設定"
    walletable_type = freee_walletable_type(walletable) or "-"
    walletable_id = freee_walletable_id(walletable) or "-"
    return f"{name}（{walletable_type} / ID:{walletable_id}）"


def format_freee_partner_label(partner: dict) -> str:
    name = partner.get("name") or partner.get("display_name") or "名称未設定"
    partner_id = partner.get("id") or "-"
    return f"{name}（ID:{partner_id}）"


def find_company_by_id(companies: list[dict], company_id: int) -> dict | None:
    for company in companies:
        if freee_int_or_none(company.get("id")) == company_id:
            return company
    return None


def fetch_freee_taxes(company_id: int, warnings: list[str]) -> list[dict]:
    try:
        data = freee_api_request("GET", f"/api/1/taxes/companies/{company_id}")
        taxes = freee_first_list(data, ("taxes", "tax_codes", "codes"))
        if taxes:
            return taxes
    except Exception as exc:
        warnings.append(
            f"税区分取得で /api/1/taxes/companies/{company_id} に失敗したため /api/1/taxes/codes にフォールバックしました。{sanitize_freee_error(str(exc))}"
        )
    data = freee_api_request("GET", "/api/1/taxes/codes", params={"company_id": company_id})
    return freee_first_list(data, ("taxes", "tax_codes", "codes"))


def fetch_freee_master_bundle(company_id: int | None = None) -> dict:
    warnings: list[str] = []
    companies = freee_list_from_response(freee_api_request("GET", "/api/1/companies"), "companies")
    common = get_freee_common_settings()
    selected_company_id = company_id
    if selected_company_id is None and common and common.get("company_id"):
        configured_id = freee_int_or_none(common.get("company_id"))
        if configured_id and find_company_by_id(companies, configured_id):
            selected_company_id = configured_id
    if selected_company_id is None and len(companies) == 1:
        selected_company_id = freee_int_or_none(companies[0].get("id"))
    if selected_company_id and not find_company_by_id(companies, selected_company_id):
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
            master[key] = freee_list_from_response(freee_api_request("GET", path, params=params), key)
        except Exception as exc:
            warnings.append(f"{key} の取得に失敗しました: {sanitize_freee_error(str(exc))}")
    try:
        master["taxes"] = fetch_freee_taxes(int(selected_company_id), warnings)
    except Exception as exc:
        warnings.append(f"税区分の取得に失敗しました: {sanitize_freee_error(str(exc))}")
    return master


def with_freee_labels(rows: list[dict], formatter) -> list[dict]:
    labeled = []
    for row in rows:
        item = dict(row)
        item["label"] = formatter(row)
        labeled.append(item)
    return labeled


def pick_uber_auto_config(master: dict) -> dict:
    warnings = list(master.get("warnings") or [])
    selected_company_id = freee_int_or_none(master.get("selected_company_id"))
    company = find_company_by_id(master.get("companies") or [], selected_company_id) if selected_company_id else None
    if not company:
        warnings.append("freee事業所を選択してください。")

    account_item = _pick_uber_account_item(master.get("account_items") or [])
    preferred_tax_code = freee_int_or_none((account_item or {}).get("default_tax_code"))
    tax = _pick_uber_tax(master.get("taxes") or [], preferred_tax_code)
    walletable = _pick_uber_walletable(master.get("walletables") or [])
    partner = _pick_uber_partner(master.get("partners") or [])
    if not account_item:
        warnings.append("勘定科目「売上高」を自動判定できませんでした。")
    if not tax:
        warnings.append("税区分「課税売上10%」を自動判定できませんでした。")
    if not walletable:
        warnings.append("決済口座「現金」を自動判定できませんでした。")

    return {
        "company_id": freee_int_or_none((company or {}).get("id")),
        "company_label": format_freee_company_label(company) if company else "",
        "account_item_id": freee_int_or_none((account_item or {}).get("id")),
        "account_item_name": (account_item or {}).get("name"),
        "tax_code": freee_tax_code(tax or {}),
        "tax_name": freee_tax_name(tax or {}),
        "walletable_type": freee_walletable_type(walletable or {}),
        "walletable_id": freee_walletable_id(walletable or {}),
        "walletable_name": (walletable or {}).get("name"),
        "partner_id": freee_int_or_none((partner or {}).get("id")),
        "partner_name": (partner or {}).get("name"),
        "partner_code": None,
        "deal_payment_mode": "settled",
        "warnings": warnings,
    }


def _pick_uber_account_item(account_items: list[dict]) -> dict | None:
    for item in account_items:
        if str(item.get("name") or "") == "売上高":
            return item
    for item in account_items:
        if "売上" in str(item.get("name") or ""):
            return item
    return None


def _pick_uber_tax(taxes: list[dict], preferred_tax_code: int | None = None) -> dict | None:
    if preferred_tax_code is not None:
        for tax in taxes:
            if freee_tax_code(tax) == preferred_tax_code:
                return tax
    for tax in taxes:
        if str(tax.get("name_ja") or "") == "課税売上10%":
            return tax
    for tax in taxes:
        if str(tax.get("name") or "") == "課税売上10%":
            return tax
    for tax in taxes:
        if freee_tax_code(tax) == 129:
            return tax
    return None


def _pick_uber_walletable(walletables: list[dict]) -> dict | None:
    for walletable in walletables:
        if str(walletable.get("name") or "") == "現金":
            return walletable
    for walletable in walletables:
        if "現金" in str(walletable.get("name") or ""):
            return walletable
    return None


def _pick_uber_partner(partners: list[dict]) -> dict | None:
    for partner in partners:
        if str(partner.get("name") or "") == "Uber":
            return partner
    for partner in partners:
        name = str(partner.get("name") or "")
        if "Uber" in name or "ウーバー" in name:
            return partner
    return None
