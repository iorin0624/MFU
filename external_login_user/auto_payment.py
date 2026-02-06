from __future__ import annotations

import secrets
import uuid
from typing import Tuple, Optional, Dict, Any

from flask import (
    request, session, redirect, url_for, render_template,
    flash, current_app, jsonify
)

from . import bp
from app.utils.db import get_db

from pathlib import Path
import os

from dotenv import load_dotenv

# ============================================================
# 環境変数読み込み（/mnt/mfu/app/external_login_user/.env）
#  ※ payment/.env よりも「こちらを優先して」使いたいので override=True
# ============================================================
BASE_DIR = Path(__file__).resolve().parent  # /mnt/mfu/app/external_login_user
ENV_FILE = BASE_DIR / ".env"
# ★ 既存の環境変数があっても external_login_user/.env で上書きする
load_dotenv(ENV_FILE, override=True)

# ここで評価される値は、external_login_user/.env によって上書きされた後のもの
SQUARE_APP_ID = os.getenv("SQUARE_APP_ID")
SQUARE_LOCATION_ID = os.getenv("SQUARE_LOCATION_ID")
SQUARE_ACCESS_TOKEN = os.getenv("SQUARE_ACCESS_TOKEN")

def _square_env_suffix(square_env: str) -> str:
    return "SANDBOX" if square_env == "SANDBOX" else "PRODUCTION"

def _load_square_creds() -> tuple[str | None, str | None, str | None]:
    square_env = _get_square_env()
    suffix = _square_env_suffix(square_env)
    app_id = os.getenv(f"SQUARE_{suffix}_APP_ID") or SQUARE_APP_ID
    location_id = os.getenv(f"SQUARE_{suffix}_LOCATION_ID") or SQUARE_LOCATION_ID
    access_token = os.getenv(f"SQUARE_{suffix}_ACCESS_TOKEN") or SQUARE_ACCESS_TOKEN
    return app_id, location_id, access_token

def _get_square_env() -> str:
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT value FROM settings WHERE `key` = 'square_env_external'")
        row = cur.fetchone()
        db.close()
        if row:
            value = row.get("value") if isinstance(row, dict) else row[0]
            if value:
                return str(value).upper()
    except Exception:
        current_app.logger.exception("[auto_payment._get_square_env] failed to load square_env")
    return os.getenv("SQUARE_ENV", "SANDBOX").upper()

def _square_js_url(square_env: str) -> str:
    return (
        "https://sandbox.web.squarecdn.com/v1/square.js"
        if square_env == "SANDBOX"
        else "https://web.squarecdn.com/v1/square.js"
    )


def is_square_config_ready() -> bool:
    """カード登録に必要な値が全部そろっているか"""
    app_id, location_id, access_token = _load_square_creds()
    return bool(app_id and location_id and access_token)


def create_square_client():
    """
    external_login_user/.env の値から
    Square の Python SDK クライアントを返す。

    ・SQUARE_APP_ID / SQUARE_LOCATION_ID / SQUARE_ACCESS_TOKEN が
      正しく読めている場合のみ有効な Client を返す。
    ・設定不備 or SDK 未インストール時は None。
    """
    # ★ 実際に「環境変数として」何が入っているかもログに出す
    try:
        env_square_env = os.getenv("SQUARE_ENV")
        env_app_id = os.getenv("SQUARE_APP_ID")
        env_location_id = os.getenv("SQUARE_LOCATION_ID")
        env_access = os.getenv("SQUARE_ACCESS_TOKEN")
        effective_env = _get_square_env()
        app_id, location_id, access_token = _load_square_creds()

        masked_token_const = None
        if access_token:
            masked_token_const = f"{access_token[:6]}... (len={len(access_token)})"

        masked_token_env = None
        if env_access:
            masked_token_env = f"{env_access[:6]}... (len={len(env_access)})"

        current_app.logger.error(
            "[auto_payment.create_square_client] CONST: ENV=%s, APP_ID=%s, LOCATION_ID=%s, ACCESS_TOKEN=%s",
            effective_env,
            app_id,
            location_id,
            masked_token_const,
        )
        current_app.logger.error(
            "[auto_payment.create_square_client] ENVVAR: ENV=%s, APP_ID=%s, LOCATION_ID=%s, ACCESS_TOKEN=%s (file=%s)",
            env_square_env,
            env_app_id,
            env_location_id,
            masked_token_env,
            str(ENV_FILE),
        )
    except Exception:
        current_app.logger.exception(
            "[auto_payment.create_square_client] debug logging failed"
        )

    if not is_square_config_ready():
        current_app.logger.error(
            "Square 設定が不足しています。（SQUARE_APP_ID / SQUARE_LOCATION_ID / SQUARE_ACCESS_TOKEN）"
        )
        return None

    app_id, location_id, access_token = _load_square_creds()

    # ★ APP_ID から sandbox / production を自動判別
    #   ・APP_ID が "sandbox-" で始まっていれば sandbox
    #   ・それ以外なら設定値（square_env / SQUARE_ENV）を見て判断
    if app_id and app_id.startswith("sandbox-"):
        environment = "sandbox"
    else:
        environment = "sandbox" if _get_square_env() == "SANDBOX" else "production"

    current_app.logger.error(
        "[auto_payment.create_square_client] EFFECTIVE_ENV=%s", environment
    )

    # レガシー SDK（squareup-legacy）前提
    try:
        from square_legacy.client import Client  # type: ignore[import-not-found]
    except Exception:
        current_app.logger.exception(
            "squareup-legacy がインストールされていないため、Square クライアントを初期化できません。"
        )
        return None

    try:
        client = Client(
            access_token=access_token,
            environment=environment,
        )
    except Exception:
        current_app.logger.exception("Square クライアントの生成に失敗しました。")
        return None

    current_app.logger.error(
        "[auto_payment.create_square_client] Square Client initialized (env=%s)",
        environment,
    )
    return client


# ============================================================
# Square クライアント
# ============================================================
def get_square_client():
    """
    external_login_user/.env の値を元に
    Square の Python SDK クライアントを取得する。

    ・SQUARE_APP_ID / SQUARE_LOCATION_ID / SQUARE_ACCESS_TOKEN が
      正しく読めている場合のみ有効な Client を返す。
    ・設定不備 or SDK 未インストール時は None。
    """
    current_app.logger.error("[auto_payment.get_square_client] called")
    client = create_square_client()
    if client is None:
        current_app.logger.error(
            "Square クライアントの初期化に失敗したため、自動決済機能を無効化します。"
        )
    return client


# ============================================================
# プロフィール用：デフォルトカード表示
# ============================================================
def load_default_card_summary(cur, user_id: int) -> Tuple[bool, Optional[str]]:
    """
    external_login_user_card_data からデフォルトカード1件を取得し、
    (has_card: bool, summary: str | None) を返す。

    summary 例:
        "VISA ****1234 (有効期限: 12/2028)"
    """
    has_card = False
    summary: Optional[str] = None

    try:
        current_app.logger.debug(
            "[auto_payment.load_default_card_summary] user_id=%s", user_id
        )
        cur.execute(
            """
            SELECT card_brand, last4, exp_month, exp_year
              FROM external_login_user_card_data
             WHERE user_id=%s
               AND deleted_at IS NULL
             ORDER BY is_default DESC, id DESC
             LIMIT 1
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            current_app.logger.debug(
                "[auto_payment.load_default_card_summary] no card found for user_id=%s",
                user_id,
            )
            return False, None

        brand = (row.get("card_brand") or "").upper()
        last4 = row.get("last4") or "****"
        mm = row.get("exp_month")
        yy = row.get("exp_year")

        txt = f"{brand} ****{last4}"
        if mm and yy:
            try:
                txt = f"{brand} ****{last4} (有効期限: {int(mm):02d}/{yy})"
            except Exception:
                current_app.logger.exception(
                    "[auto_payment.load_default_card_summary] failed to format expiry mm=%r, yy=%r",
                    mm,
                    yy,
                )

        has_card = True
        summary = txt
        current_app.logger.debug(
            "[auto_payment.load_default_card_summary] has_card=%s, summary=%s",
            has_card,
            summary,
        )
    except Exception:
        current_app.logger.exception("load_default_card_summary failed")

    return has_card, summary


# ============================================================
# カード登録画面（GET）
# ============================================================
@bp.route("/card", methods=["GET"])
def card():
    """
    自動決済用カードを登録する画面。
    → HTML 側で Square Web Payments SDK を読み込み、カード番号入力はそっちで全部やる。
    """
    current_app.logger.info("[auto_payment.card] GET /card called")

    social_id = session.get("ext_user_social_id")
    current_app.logger.debug(
        "[auto_payment.card] session.ext_user_social_id=%r", social_id
    )
    if not social_id:
        return redirect(url_for(
            "external_login_user.line_login",
            next=session.get("ext_after_login_next") or request.url
        ))

    # CSRF トークン
    if "ext_csrf" not in session:
        session["ext_csrf"] = secrets.token_hex(16)
        current_app.logger.debug("[auto_payment.card] generated new ext_csrf")
    csrf_token = session["ext_csrf"]

    db = get_db()
    cur = db.cursor(dictionary=True)

    # ログインユーザー取得
    cur.execute(
        """
        SELECT id, nickname, email
          FROM external_login_user
         WHERE social_id=%s
         LIMIT 1
        """,
        (social_id,),
    )
    me = cur.fetchone()
    current_app.logger.debug("[auto_payment.card] me=%r", me)
    if not me:
        cur.close()
        db.close()
        return redirect(url_for(
            "external_login_user.line_login",
            next=session.get("ext_after_login_next") or request.url
        ))

    # 既存カードサマリ
    has_card, card_summary = load_default_card_summary(cur, me["id"])

    cur.close()
    db.close()

    # Square の AppId / LocationId は external_login_user/.env から読み込んだ値を渡す
    square_app_id, square_location_id, _ = _load_square_creds()
    current_app.logger.debug(
        "[auto_payment.card] square_app_id=%r, square_location_id=%r",
        square_app_id,
        square_location_id,
    )

    # 設定不足があればフラッシュ
    if not is_square_config_ready():
        flash(
            "カード登録の設定が未完了です。（SQUARE_APP_ID / SQUARE_LOCATION_ID / SQUARE_ACCESS_TOKEN を確認してください）",
            "danger"
        )
        current_app.logger.warning(
            "[auto_payment.card] square config is NOT ready"
        )
    else:
        current_app.logger.info(
            "[auto_payment.card] square config looks OK"
        )

    square_env = _get_square_env()
    return render_template(
        "ext_card.html",  # ★ テンプレート名はそのまま
        me=me,
        csrf_token=csrf_token,
        square_app_id=square_app_id,
        square_location_id=square_location_id,
        square_js_url=_square_js_url(square_env),
        square_env=square_env,
        has_card=has_card,
        card_summary=card_summary,
    )


# ============================================================
# カード登録処理（JS からの token を受ける API）
# ============================================================
@bp.route("/card/token", methods=["POST"])
def card_token():
    """
    Web Payments SDK から送られてくる token を受け取り、
    Customers API + Cards API で card on file を作成し、
    external_login_user_card_data に保存する。
    """
    current_app.logger.error("[auto_payment.card_token] POST /card/token called")

    if not request.is_json:
        current_app.logger.error(
            "[auto_payment.card_token] request is not JSON (Content-Type=%r)",
            request.headers.get("Content-Type"),
        )
        return jsonify({"ok": False, "message": "JSON 形式で送信してください。"}), 400

    data: Dict[str, Any] = request.get_json(silent=True) or {}
    current_app.logger.error("[auto_payment.card_token] request.json=%r", data)

    token = (data.get("token") or "").strip()
    req_csrf = (data.get("csrf_token") or "").strip()

    if token:
        safe_token_preview = f"{token[:10]}... (len={len(token)})"
    else:
        safe_token_preview = "<EMPTY>"

    current_app.logger.error(
        "[auto_payment.card_token] received token preview=%s, csrf_token=%r",
        safe_token_preview,
        req_csrf,
    )

    if not token:
        current_app.logger.error(
            "[auto_payment.card_token] token is empty"
        )
        return jsonify({"ok": False, "message": "token が空です。"}), 400

    if not req_csrf or req_csrf != session.get("ext_csrf"):
        current_app.logger.error(
            "[auto_payment.card_token] CSRF mismatch: req=%r, sess=%r",
            req_csrf,
            session.get("ext_csrf"),
        )
        return jsonify({"ok": False, "message": "CSRF 検証エラーです。再読み込みしてください。"}), 400

    social_id = session.get("ext_user_social_id")
    current_app.logger.debug(
        "[auto_payment.card_token] session.ext_user_social_id=%r",
        social_id,
    )
    if not social_id:
        return jsonify({"ok": False, "message": "ログインが切れています。再ログインしてください。"}), 401

    db = get_db()
    cur = db.cursor(dictionary=True)

    # ログインユーザー取得
    cur.execute(
        """
        SELECT id, nickname, email
          FROM external_login_user
         WHERE social_id=%s
         LIMIT 1
        """,
        (social_id,),
    )
    me = cur.fetchone()
    current_app.logger.debug("[auto_payment.card_token] me=%r", me)
    if not me:
        cur.close()
        db.close()
        return jsonify({"ok": False, "message": "ユーザー情報が見つかりません。"}), 404

    user_id = me["id"]

    # 既存の Square カスタマーID があれば再利用
    cur.execute(
        """
        SELECT square_customer_id
          FROM external_login_user_card_data
         WHERE user_id=%s
           AND deleted_at IS NULL
           AND square_customer_id IS NOT NULL
         ORDER BY id ASC
         LIMIT 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    square_customer_id: Optional[str] = row["square_customer_id"] if row else None
    current_app.logger.debug(
        "[auto_payment.card_token] existing square_customer_id=%r",
        square_customer_id,
    )

    client = get_square_client()
    if client is None:
        current_app.logger.error(
            "[auto_payment.card_token] get_square_client returned None"
        )
        cur.close()
        db.close()
        return jsonify({
            "ok": False,
            "message": "Square 連携がまだ有効化されていないため、カード登録は行えません。"
        }), 503

    customers_api = client.customers
    cards_api = client.cards

    # --- カスタマーがなければ作成 ---
    if not square_customer_id:
        current_app.logger.info(
            "[auto_payment.card_token] no square_customer_id, creating new customer..."
        )
        try:
            body = {
                "given_name": me.get("nickname") or None,
                "email_address": me.get("email") or None,
            }
            current_app.logger.debug(
                "[auto_payment.card_token] create_customer body=%r", body
            )
            result = customers_api.create_customer(body)
            if result.is_error():
                current_app.logger.error(
                    "Square create_customer error: %s", result.errors
                )
                cur.close()
                db.close()
                return jsonify({
                    "ok": False,
                    "message": "カード登録に失敗しました。（顧客作成エラー）"
                }), 502

            square_customer_id = result.body["customer"]["id"]
            current_app.logger.info(
                "[auto_payment.card_token] created customer_id=%s",
                square_customer_id,
            )
        except Exception:
            current_app.logger.exception("Square create_customer exception")
            cur.close()
            db.close()
            return jsonify({
                "ok": False,
                "message": "カード登録に失敗しました。（顧客作成例外）"
            }), 502

    # --- カード on file を作成 ---
    try:
        idempotency_key = str(uuid.uuid4())
        current_app.logger.debug(
            "[auto_payment.card_token] idempotency_key=%s", idempotency_key
        )

        body = {
            "idempotency_key": idempotency_key,
            "source_id": token,  # Web Payments SDK の payment token
            "card": {
                "customer_id": square_customer_id,
            }
        }

        masked_body = {
            **body,
            "source_id": safe_token_preview,
        }
        current_app.logger.debug(
            "[auto_payment.card_token] create_card body (masked)=%r",
            masked_body,
        )

        result = cards_api.create_card(body)
        if result.is_error():
            current_app.logger.error(
                "Square create_card error: %s", result.errors
            )
            cur.close()
            db.close()
            return jsonify({
                "ok": False,
                "message": "カード登録に失敗しました。（カード保存エラー）"
            }), 502

        card = result.body["card"]
        square_card_id = card["id"]
        card_brand = card.get("card_brand")
        last4 = card.get("last_4")
        exp_month = card.get("exp_month")
        exp_year = card.get("exp_year")

        current_app.logger.info(
            "[auto_payment.card_token] card created: card_id=%s, brand=%r, last4=%r, exp_month=%r, exp_year=%r",
            square_card_id,
            card_brand,
            last4,
            exp_month,
            exp_year,
        )

        # DB 反映
        try:
            cur.execute(
                """
                UPDATE external_login_user_card_data
                   SET is_default = 0
                 WHERE user_id=%s
                """,
                (user_id,),
            )

            cur.execute(
                """
                INSERT INTO external_login_user_card_data (
                    user_id,
                    square_customer_id,
                    square_card_id,
                    card_brand,
                    last4,
                    exp_month,
                    exp_year,
                    is_default,
                    created_at,
                    updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,1,NOW(),NOW())
                """,
                (
                    user_id,
                    square_customer_id,
                    square_card_id,
                    card_brand or None,
                    last4 or None,
                    exp_month,
                    exp_year,
                ),
            )
            db.commit()
            current_app.logger.info(
                "[auto_payment.card_token] card info saved into DB (user_id=%s, square_card_id=%s)",
                user_id,
                square_card_id,
            )
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            current_app.logger.exception(
                "DB insert external_login_user_card_data failed"
            )
            cur.close()
            db.close()
            return jsonify({
                "ok": False,
                "message": "カード登録に失敗しました。（DBエラー）"
            }), 500

        cur.close()
        db.close()
        return jsonify({"ok": True})

    except Exception:
        current_app.logger.exception("Square create_card exception")
        cur.close()
        db.close()
        return jsonify({
            "ok": False,
            "message": "カード登録に失敗しました。（カード保存例外）"
        }), 502
