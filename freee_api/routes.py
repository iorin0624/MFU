from __future__ import annotations

import secrets
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import flash, jsonify, redirect, render_template, request, session, url_for

from . import freee_api_bp
from .models import ensure_freee_api_schema
from .services import (
    FREEE_AUTHORIZE_URL,
    FREEE_TOKEN_URL,
    UBER_INTEGRATION_KEY,
    INVOICE_INTEGRATION_KEY,
    fetch_freee_master_bundle,
    find_company_by_id,
    format_freee_account_item_label,
    format_freee_company_label,
    format_freee_partner_label,
    format_freee_tax_label,
    format_freee_walletable_label,
    freee_api_request,
    freee_error_from_response,
    freee_list_from_response,
    freee_oauth_config,
    get_freee_common_settings,
    get_freee_integration_settings,
    load_freee_token_row,
    parse_optional_int_value,
    pick_uber_auto_config,
    sanitize_freee_error,
    save_freee_tokens,
    upsert_freee_common_settings,
    upsert_freee_integration_settings,
    validate_freee_common_settings,
    validate_freee_invoice_settings,
    validate_freee_integration_settings,
    with_freee_labels,
)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            flash("ログインが必要です。", "warning")
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


def _is_admin_user() -> bool:
    return session.get("user") == "admin"


def _require_admin():
    if not _is_admin_user():
        flash("管理者のみ操作できます。", "warning")
        return redirect(url_for("records.index"))
    return None


def _require_admin_json():
    if not _is_admin_user():
        return jsonify({"ok": False, "message": "管理者のみ操作できます。"}), 403
    return None


@freee_api_bp.before_request
def _init_freee_api_schema() -> None:
    ensure_freee_api_schema()


@freee_api_bp.get("/")
@login_required
def index():
    admin_redirect = _require_admin()
    if admin_redirect:
        return admin_redirect
    common_settings = get_freee_common_settings()
    uber_settings = get_freee_integration_settings(UBER_INTEGRATION_KEY)
    invoice_settings = get_freee_integration_settings(INVOICE_INTEGRATION_KEY)
    common_error = validate_freee_common_settings(common_settings)
    uber_error = validate_freee_integration_settings(uber_settings)
    invoice_error = validate_freee_invoice_settings(invoice_settings)
    return render_template(
        "freee_api/index.html",
        freee_connected=bool(load_freee_token_row()),
        common_settings=common_settings,
        uber_settings=uber_settings,
        invoice_settings=invoice_settings,
        common_settings_complete=not common_error,
        uber_settings_complete=not uber_error,
        invoice_settings_complete=not invoice_error,
        common_settings_error=common_error,
        uber_settings_error=uber_error,
        invoice_settings_error=invoice_error,
    )


@freee_api_bp.get("/connect")
@login_required
def connect():
    admin_redirect = _require_admin()
    if admin_redirect:
        return admin_redirect
    config = freee_oauth_config()
    if not config["client_id"]:
        flash("FREEE_CLIENT_ID が未設定です。", "warning")
        return redirect(url_for("freee_api.index"))
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


@freee_api_bp.get("/callback")
@login_required
def callback():
    admin_redirect = _require_admin()
    if admin_redirect:
        return admin_redirect
    if request.args.get("error"):
        flash(f"freee接続がキャンセルまたは失敗しました: {request.args.get('error')}", "warning")
        return redirect(url_for("freee_api.index"))
    expected_state = session.pop("freee_oauth_state", None)
    actual_state = request.args.get("state")
    if not expected_state or actual_state != expected_state:
        flash("freee OAuth state が一致しません。もう一度接続してください。", "danger")
        return redirect(url_for("freee_api.index"))
    code = request.args.get("code")
    if not code:
        flash("freee OAuth code が取得できませんでした。", "danger")
        return redirect(url_for("freee_api.index"))
    config = freee_oauth_config()
    if not config["client_id"] or not config["client_secret"]:
        flash("FREEE_CLIENT_ID / FREEE_CLIENT_SECRET が未設定です。", "warning")
        return redirect(url_for("freee_api.index"))
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
            raise RuntimeError(freee_error_from_response(resp))
        save_freee_tokens(resp.json())
        flash("freeeに接続しました。", "success")
    except Exception as exc:
        flash(sanitize_freee_error(str(exc)), "danger")
    return redirect(url_for("freee_api.index"))


@freee_api_bp.post("/settings")
@login_required
def save_settings():
    admin_redirect = _require_admin()
    if admin_redirect:
        return admin_redirect
    source = request.form
    try:
        company_id = parse_optional_int_value(source.get("company_id"))
        account_item_id = parse_optional_int_value(source.get("account_item_id"))
        tax_code = parse_optional_int_value(source.get("tax_code"))
        deal_payment_mode = (source.get("deal_payment_mode") or "settled").strip()
        walletable_type = (source.get("walletable_type") or "").strip() or None
        walletable_id = parse_optional_int_value(source.get("walletable_id"))
        walletable_value = (source.get("walletable") or "").strip()
        if walletable_value:
            raw_type, sep, raw_id = walletable_value.partition(":")
            if sep:
                walletable_type = raw_type.strip() or None
                walletable_id = parse_optional_int_value(raw_id)
        partner_id = parse_optional_int_value(source.get("partner_id"))
        partner_code = (source.get("partner_code") or "").strip() or None
        invoice_account_item_id = parse_optional_int_value(source.get("invoice_account_item_id"))
        invoice_tax_code = parse_optional_int_value(source.get("invoice_tax_code"))
        invoice_tax_code_8 = parse_optional_int_value(source.get("invoice_tax_code_8"))
        invoice_tax_code_nontax = parse_optional_int_value(source.get("invoice_tax_code_nontax"))
        invoice_walletable_type = (source.get("invoice_walletable_type") or "").strip() or None
        invoice_walletable_id = parse_optional_int_value(source.get("invoice_walletable_id"))
        invoice_walletable_value = (source.get("invoice_walletable") or "").strip()
        if invoice_walletable_value:
            raw_type, sep, raw_id = invoice_walletable_value.partition(":")
            if sep:
                invoice_walletable_type = raw_type.strip() or None
                invoice_walletable_id = parse_optional_int_value(raw_id)
    except (TypeError, ValueError):
        flash("freee設定の数値項目を確認してください。", "warning")
        return redirect(url_for("freee_api.index"))

    common = {"company_id": company_id}
    uber = {
        "account_item_id": account_item_id,
        "tax_code": tax_code,
        "deal_payment_mode": deal_payment_mode,
        "walletable_type": walletable_type,
        "walletable_id": walletable_id,
        "partner_id": partner_id,
        "partner_code": partner_code,
    }
    invoice = {
        "account_item_id": invoice_account_item_id,
        "tax_code": invoice_tax_code,
        "tax_code_8": invoice_tax_code_8,
        "tax_code_nontax": invoice_tax_code_nontax,
        "deal_payment_mode": "settled",
        "walletable_type": invoice_walletable_type,
        "walletable_id": invoice_walletable_id,
        "partner_id": None,
        "partner_code": None,
    }
    common_error = validate_freee_common_settings(common)
    uber_error = validate_freee_integration_settings(uber)
    invoice_error = validate_freee_invoice_settings(invoice)
    if common_error or uber_error or invoice_error:
        flash(common_error or uber_error or invoice_error, "warning")
        return redirect(url_for("freee_api.index"))
    if company_id and load_freee_token_row():
        try:
            companies = freee_list_from_response(freee_api_request("GET", "/api/1/companies"), "companies")
            if not find_company_by_id(companies, int(company_id)):
                matched_number = any(str(company.get("company_number") or "") == str(company_id) for company in companies)
                if matched_number:
                    flash("事業所IDには会社番号ではなく、freee内部IDを選択してください。", "warning")
                    return redirect(url_for("freee_api.index"))
        except Exception:
            pass
    upsert_freee_common_settings(**common)
    upsert_freee_integration_settings(integration_key=UBER_INTEGRATION_KEY, **uber)
    upsert_freee_integration_settings(integration_key=INVOICE_INTEGRATION_KEY, **invoice)
    flash("freee設定を保存しました。", "success")
    return redirect(url_for("freee_api.index"))


@freee_api_bp.get("/master-data")
@login_required
def master_data():
    admin_error = _require_admin_json()
    if admin_error:
        return admin_error
    common = get_freee_common_settings()
    raw_company_id = request.args.get("company_id") or (common or {}).get("company_id")
    try:
        company_id = int(raw_company_id) if raw_company_id else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "company_id を確認してください。"}), 400
    try:
        master = fetch_freee_master_bundle(company_id)
        return jsonify(
            {
                "ok": True,
                "companies": master.get("companies") or [],
                "account_items": master.get("account_items") or [],
                "partners": master.get("partners") or [],
                "walletables": master.get("walletables") or [],
                "taxes": master.get("taxes") or [],
                "warnings": master.get("warnings") or [],
                "selected_company_id": master.get("selected_company_id"),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "message": sanitize_freee_error(str(exc))}), 500


@freee_api_bp.get("/auto-config-candidates")
@login_required
def auto_config_candidates():
    admin_error = _require_admin_json()
    if admin_error:
        return admin_error
    if not load_freee_token_row():
        return jsonify({"ok": False, "message": "freeeに接続されていません。先にfreee接続を行ってください。"}), 401
    try:
        raw_company_id = request.args.get("company_id")
        company_id = int(raw_company_id) if raw_company_id else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "freee事業所を選択してください。"}), 400
    try:
        master = fetch_freee_master_bundle(company_id)
        suggested = pick_uber_auto_config(master)
        return jsonify(
            {
                "ok": True,
                "companies": with_freee_labels(master.get("companies") or [], format_freee_company_label),
                "account_items": with_freee_labels(master.get("account_items") or [], format_freee_account_item_label),
                "taxes": with_freee_labels(master.get("taxes") or [], format_freee_tax_label),
                "walletables": with_freee_labels(master.get("walletables") or [], format_freee_walletable_label),
                "partners": with_freee_labels(master.get("partners") or [], format_freee_partner_label),
                "selected_company_id": master.get("selected_company_id"),
                "suggested": suggested,
                "warnings": list(master.get("warnings") or []) + list(suggested.get("warnings") or []),
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "message": sanitize_freee_error(str(exc))}), 500


@freee_api_bp.post("/auto-config-save")
@login_required
def auto_config_save():
    admin_error = _require_admin_json()
    if admin_error:
        return admin_error
    if not load_freee_token_row():
        return jsonify({"ok": False, "message": "freeeに接続されていません。先にfreee接続を行ってください。"}), 401
    payload = request.get_json(silent=True) or {}
    try:
        company_id = int(payload.get("company_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "freee事業所を選択してください。"}), 400
    if company_id <= 0:
        return jsonify({"ok": False, "message": "freee事業所を選択してください。"}), 400
    try:
        master = fetch_freee_master_bundle(company_id)
        suggested = pick_uber_auto_config(master)
        common = {"company_id": suggested.get("company_id")}
        uber = {
            "account_item_id": suggested.get("account_item_id"),
            "tax_code": suggested.get("tax_code"),
            "deal_payment_mode": suggested.get("deal_payment_mode") or "settled",
            "walletable_type": suggested.get("walletable_type"),
            "walletable_id": suggested.get("walletable_id"),
            "partner_id": suggested.get("partner_id"),
            "partner_code": suggested.get("partner_code"),
        }
        warnings = list(master.get("warnings") or []) + list(suggested.get("warnings") or [])
        if validate_freee_common_settings(common) or validate_freee_integration_settings(uber):
            return jsonify(
                {
                    "ok": False,
                    "message": "自動設定に必要な勘定科目、税区分、または決済口座が見つかりませんでした。一覧から手動選択してください。",
                    "warnings": warnings,
                    "suggested": suggested,
                }
            ), 400
        upsert_freee_common_settings(**common)
        upsert_freee_integration_settings(integration_key=UBER_INTEGRATION_KEY, **uber)
        return jsonify(
            {
                "ok": True,
                "message": "freee設定を自動保存しました。",
                "settings": {
                    **common,
                    **uber,
                    "account_item_name": suggested.get("account_item_name"),
                    "tax_name": suggested.get("tax_name"),
                    "walletable_name": suggested.get("walletable_name"),
                    "partner_name": suggested.get("partner_name"),
                },
                "warnings": warnings,
            }
        )
    except Exception as exc:
        return jsonify({"ok": False, "message": sanitize_freee_error(str(exc))}), 500
