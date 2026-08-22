from __future__ import annotations

import time
from datetime import datetime, timedelta

import bcrypt
import requests
from flask import current_app, flash, redirect, render_template, request, session, url_for

from app import admin_required
from app.utils.db import get_db

from . import bank_account_bp
from .token_service import (
    build_payout_access_url,
    create_payout_access_token,
    get_payout_access_token_by_id,
    is_payout_access_token_usable,
    toggle_payout_access_token_active,
    touch_payout_access_token_usage,
    verify_payout_access_token,
)

MAX_UNLOCK_FAILURES = 10
LOCK_SECONDS = 10 * 60
NEW_ACCESS_TOKEN_SESSION_KEY = "payout_new_access_token"
PAYPAY_LINK_EXPIRE_DAYS = 13
USER_PAYPAY_EXPIRED_WARNING_MESSAGE = "このリンクは有効期限が切れているようです。お手数ですが、ご本人にご連絡ください。"



def _get_settings(cursor):
    cursor.execute(
        """
        SELECT id, payout_password_hash, payout_password_version, account_holder_name, updated_at
        FROM mfu_payout_settings
        WHERE id = 1
        LIMIT 1
        """
    )
    return cursor.fetchone() or {
        "id": 1,
        "payout_password_hash": None,
        "payout_password_version": 1,
        "account_holder_name": None,
    }



def _clear_unlock_failure() -> None:
    session.pop("payout_unlock_fail_count", None)
    session.pop("payout_unlock_lock_until", None)


def _clear_payout_token_session() -> None:
    if session.get("payout_auth_method") == "token":
        session.pop("payout_unlocked", None)
    session.pop("payout_auth_method", None)
    session.pop("payout_token_id", None)



def _clear_payout_auth_session(clear_failure: bool = False) -> None:
    session.pop("payout_unlocked", None)
    session.pop("payout_pwd_version", None)
    _clear_payout_token_session()
    if clear_failure:
        _clear_unlock_failure()



def _is_password_unlocked(current_version: int) -> bool:
    return (
        session.get("payout_unlocked") is True
        and session.get("payout_auth_method") in (None, "password")
        and session.get("payout_pwd_version") == current_version
    )



def _is_token_session_unlocked(db) -> bool:
    if session.get("payout_unlocked") is not True:
        return False
    if session.get("payout_auth_method") != "token":
        return False

    token_id = session.get("payout_token_id")
    if not token_id:
        return False

    token_row = get_payout_access_token_by_id(db, int(token_id))
    if not is_payout_access_token_usable(token_row):
        _clear_payout_token_session()
        return False
    return True



def _is_any_payout_unlocked(db, current_version: int) -> bool:
    return _is_password_unlocked(current_version) or _is_token_session_unlocked(db)



def _client_ip() -> str | None:
    forwarded_for = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded_for or request.remote_addr



def _consume_new_access_token_notice() -> dict | None:
    return session.pop(NEW_ACCESS_TOKEN_SESSION_KEY, None)



def _set_new_access_token_notice(created: dict) -> None:
    session[NEW_ACCESS_TOKEN_SESSION_KEY] = {
        "id": created.get("id"),
        "token": created.get("token"),
        "token_preview": created.get("token_preview"),
        "access_url": created.get("access_url"),
        "memo": created.get("memo"),
        "created_at": created.get("created_at").strftime("%Y-%m-%d %H:%M:%S") if created.get("created_at") else None,
    }


def _format_datetime(value: datetime | None) -> str:
    if not value:
        return "未設定"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _build_paypay_link_status(paypay_row, now=None) -> dict:
    now_dt = now or datetime.now()
    paypay = paypay_row or {}
    link = (paypay.get("paypay_link") or "").strip()
    saved_at = paypay.get("paypay_link_saved_at")
    is_active = int(paypay.get("is_active") or 0) == 1
    has_link = bool(link)
    is_expired = False
    elapsed_days = None

    if saved_at:
        elapsed = now_dt - saved_at
        elapsed_days = max(0, int(elapsed.total_seconds() // 86400))
        if has_link and is_active and now_dt >= (saved_at + timedelta(days=PAYPAY_LINK_EXPIRE_DAYS)):
            is_expired = True

    return {
        "has_link": has_link,
        "is_active": is_active,
        "saved_at": saved_at,
        "saved_at_display": _format_datetime(saved_at),
        "elapsed_days": elapsed_days,
        "elapsed_days_display": f"{elapsed_days}日" if elapsed_days is not None else "未設定",
        "is_expired": is_expired,
        "warning_message": USER_PAYPAY_EXPIRED_WARNING_MESSAGE if is_expired else None,
        "expires_after_days": PAYPAY_LINK_EXPIRE_DAYS,
    }


def _get_admin_webhook_urls(cursor) -> list[str]:
    cursor.execute(
        """
        SELECT webhook_url
        FROM users
        WHERE username = 'admin'
          AND webhook_url IS NOT NULL
          AND webhook_url <> ''
        """
    )
    rows = cursor.fetchall() or []
    urls: list[str] = []
    for row in rows:
        webhook_url = row.get("webhook_url") if isinstance(row, dict) else row[0]
        if webhook_url and webhook_url not in urls:
            urls.append(webhook_url)
    from app.discord_notifications.repository import get_discord_webhook
    resolved = get_discord_webhook("paypay_payout_expiry", urls[0] if urls else "")
    return [resolved] if resolved else []


def _post_discord_webhook(webhook_url: str, *, title: str, description: str, fields=(), color: int = 0xF1C40F) -> bool:
    payload = {
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "fields": [{"name": name, "value": value, "inline": inline} for (name, value, inline) in fields],
            }
        ]
    }
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
    return True


def _notify_expired_paypay_link_if_needed(db, paypay_row, paypay_status: dict) -> None:
    if not paypay_status.get("is_expired"):
        return
    if paypay_row.get("paypay_link_expired_notified_at") is not None:
        return

    cursor = db.cursor(dictionary=True)
    try:
        webhook_urls = _get_admin_webhook_urls(cursor)
        if not webhook_urls:
            return

        description = "利用者が /payout を開いた際、PayPayリンクが期限切れ扱い（13日超過）でした。"
        fields = (
            ("保存日時", paypay_status.get("saved_at_display") or "未設定", False),
            ("経過日数", paypay_status.get("elapsed_days_display") or "未設定", True),
            ("IP", _client_ip() or "不明", True),
            ("ページ", "/payout", True),
        )

        success_count = 0
        for webhook_url in webhook_urls:
            try:
                if _post_discord_webhook(
                    webhook_url,
                    title="PayPayリンク期限警告",
                    description=description,
                    fields=fields,
                ):
                    success_count += 1
            except Exception:
                current_app.logger.exception("PayPayリンク期限切れDiscord通知に失敗しました: webhook=%s", webhook_url)

        if success_count > 0:
            cursor.execute(
                """
                UPDATE mfu_payout_paypay
                SET paypay_link_expired_notified_at = NOW()
                WHERE id = %s
                  AND paypay_link_expired_notified_at IS NULL
                """,
                (paypay_row["id"],),
            )
            db.commit()
    except Exception:
        current_app.logger.exception("PayPayリンク期限切れ通知処理でエラーが発生しました")
    finally:
        cursor.close()


@bank_account_bp.route("/payout")
def payout_index():
    iv = (request.args.get("iv") or "").strip()
    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        settings = _get_settings(cursor)
        version = int(settings.get("payout_password_version") or 1)

        if iv:
            token_row = verify_payout_access_token(db, iv)
            if token_row:
                session["payout_unlocked"] = True
                session["payout_auth_method"] = "token"
                session["payout_token_id"] = int(token_row["id"])
                session.pop("payout_pwd_version", None)
                touch_payout_access_token_usage(db, int(token_row["id"]), _client_ip())
                _clear_unlock_failure()
                return redirect(url_for("bank_account.payout_index"))

            _clear_payout_token_session()
            flash("アクセストークンが無効です。", "danger")
            return redirect(url_for("bank_account.payout_unlock"))

        if not _is_any_payout_unlocked(db, version):
            _clear_payout_auth_session()
            return redirect(
                url_for(
                    "bank_account.payout_unlock",
                    setup_required=1 if not settings.get("payout_password_hash") else None,
                )
            )

        cursor.execute(
            """
            SELECT id, label, bank_name, branch_name, account_number, sort_order
            FROM mfu_payout_account
            WHERE is_active = 1
            ORDER BY sort_order ASC, id ASC
            """
        )
        accounts = cursor.fetchall()

        cursor.execute(
            """
            SELECT id,
                   paypay_send_id,
                   paypay_link,
                   is_active,
                   paypay_link_saved_at,
                   paypay_link_expired_notified_at
            FROM mfu_payout_paypay
            WHERE id = 1
            LIMIT 1
            """
        )
        paypay = cursor.fetchone() or {}
        paypay_status = _build_paypay_link_status(paypay)
        _notify_expired_paypay_link_if_needed(db, paypay, paypay_status)
    finally:
        cursor.close()
        db.close()

    return render_template(
        "bank_account/index.html",
        accounts=accounts,
        paypay=paypay,
        paypay_status=paypay_status,
        account_holder_name=settings.get("account_holder_name"),
    )


@bank_account_bp.route("/payout/unlock", methods=["GET", "POST"])
def payout_unlock():
    now_ts = int(time.time())
    lock_until = int(session.get("payout_unlock_lock_until") or 0)
    lock_remaining = max(0, lock_until - now_ts)

    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        settings = _get_settings(cursor)
        pwd_hash = settings.get("payout_password_hash")
        version = int(settings.get("payout_password_version") or 1)
        if request.method == "POST":
            if lock_remaining > 0:
                flash(f"失敗回数が上限に達しました。約{lock_remaining // 60 + 1}分後に再試行してください。", "danger")
                return redirect(url_for("bank_account.payout_unlock"))

            password = request.form.get("password") or ""
            if not password:
                flash("パスワードを入力してください。", "danger")
                return redirect(url_for("bank_account.payout_unlock"))

            if not pwd_hash:
                flash("管理者がまだパスワードを設定していません。", "warning")
                return redirect(url_for("bank_account.payout_unlock"))

            submitted = password.encode("utf-8")
            stored = pwd_hash.encode("utf-8")
            if bcrypt.checkpw(submitted, stored):
                session["payout_unlocked"] = True
                session["payout_pwd_version"] = version
                session["payout_auth_method"] = "password"
                session.pop("payout_token_id", None)
                _clear_unlock_failure()
                flash("振込先ページの閲覧を許可しました。", "success")
                return redirect(url_for("bank_account.payout_index"))

            fail_count = int(session.get("payout_unlock_fail_count") or 0) + 1
            session["payout_unlock_fail_count"] = fail_count
            if fail_count >= MAX_UNLOCK_FAILURES:
                session["payout_unlock_lock_until"] = now_ts + LOCK_SECONDS
                flash("失敗が10回に達したため、10分間ロックしました。", "danger")
            else:
                flash(f"パスワードが違います。（{fail_count}/{MAX_UNLOCK_FAILURES}）", "danger")
            return redirect(url_for("bank_account.payout_unlock"))
    finally:
        cursor.close()
        db.close()

    return render_template(
        "bank_account/unlock.html",
        password_is_set=bool(settings.get("payout_password_hash")),
        lock_remaining=lock_remaining,
        setup_required=request.args.get("setup_required"),
    )


@bank_account_bp.route("/payout/logout")
def payout_logout():
    _clear_payout_auth_session(clear_failure=False)
    flash("振込先ページの閲覧許可を解除しました。", "info")
    return redirect(url_for("bank_account.payout_unlock"))


@bank_account_bp.route("/admin/payout", methods=["GET", "POST"])
@admin_required
def admin_payout():
    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        if request.method == "POST":
            action = request.form.get("action") or ""

            if action == "update_holder_name":
                account_holder_name = (request.form.get("account_holder_name") or "").strip() or None
                cursor.execute(
                    """
                    UPDATE mfu_payout_settings
                    SET account_holder_name = %s,
                        updated_at = NOW()
                    WHERE id = 1
                    """,
                    (account_holder_name,),
                )
                db.commit()
                flash("銀行口座の名義を更新しました。", "success")

            elif action == "update_paypay":
                send_id = (request.form.get("paypay_send_id") or "").strip() or None
                link = (request.form.get("paypay_link") or "").strip() or None
                is_active = 1 if request.form.get("is_active") == "1" else 0
                if link:
                    saved_at_sql = "NOW()"
                    notified_at_sql = "NULL"
                    params = (send_id, link, is_active)
                else:
                    saved_at_sql = "NULL"
                    notified_at_sql = "NULL"
                    params = (send_id, None, is_active)
                cursor.execute(
                    f"""
                    UPDATE mfu_payout_paypay
                    SET paypay_send_id = %s,
                        paypay_link = %s,
                        is_active = %s,
                        paypay_link_saved_at = {saved_at_sql},
                        paypay_link_expired_notified_at = {notified_at_sql},
                        updated_at = NOW()
                    WHERE id = 1
                    """,
                    params,
                )
                db.commit()
                flash("PayPay設定を更新しました。", "success")

            elif action == "add_account":
                label = (request.form.get("label") or "").strip() or None
                bank_name = (request.form.get("bank_name") or "").strip()
                branch_name = (request.form.get("branch_name") or "").strip() or None
                account_number = (request.form.get("account_number") or "").strip()
                sort_order = int(request.form.get("sort_order") or 0)
                is_active = 1 if request.form.get("is_active") == "1" else 0

                if not bank_name or not account_number:
                    flash("銀行名と口座番号は必須です。", "danger")
                else:
                    cursor.execute(
                        """
                        INSERT INTO mfu_payout_account
                            (label, bank_name, branch_name, account_number, is_active, sort_order, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
                        """,
                        (label, bank_name, branch_name, account_number, is_active, sort_order),
                    )
                    db.commit()
                    flash("口座を追加しました。", "success")

            elif action == "update_account":
                account_id = int(request.form.get("account_id") or 0)
                label = (request.form.get("label") or "").strip() or None
                bank_name = (request.form.get("bank_name") or "").strip()
                branch_name = (request.form.get("branch_name") or "").strip() or None
                account_number = (request.form.get("account_number") or "").strip()
                sort_order = int(request.form.get("sort_order") or 0)
                is_active = 1 if request.form.get("is_active") == "1" else 0

                if not account_id or not bank_name or not account_number:
                    flash("口座更新に必要な項目が不足しています。", "danger")
                else:
                    cursor.execute(
                        """
                        UPDATE mfu_payout_account
                        SET label = %s,
                            bank_name = %s,
                            branch_name = %s,
                            account_number = %s,
                            is_active = %s,
                            sort_order = %s,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (label, bank_name, branch_name, account_number, is_active, sort_order, account_id),
                    )
                    db.commit()
                    flash("口座を更新しました。", "success")

            elif action == "deactivate_account":
                account_id = int(request.form.get("account_id") or 0)
                if account_id:
                    cursor.execute(
                        """
                        UPDATE mfu_payout_account
                        SET is_active = 0,
                            updated_at = NOW()
                        WHERE id = %s
                        """,
                        (account_id,),
                    )
                    db.commit()
                    flash("口座を無効化しました。", "info")

            elif action == "change_password":
                new_password = request.form.get("new_password") or ""
                confirm = request.form.get("confirm_password") or ""
                if not new_password:
                    flash("新しいパスワードを入力してください。", "danger")
                elif new_password != confirm:
                    flash("確認用パスワードが一致しません。", "danger")
                else:
                    new_hash = bcrypt.hashpw(
                        new_password.encode("utf-8"), bcrypt.gensalt()
                    ).decode("utf-8")
                    cursor.execute(
                        """
                        UPDATE mfu_payout_settings
                        SET payout_password_hash = %s,
                            payout_password_version = payout_password_version + 1,
                            updated_at = NOW()
                        WHERE id = 1
                        """,
                        (new_hash,),
                    )
                    db.commit()
                    flash("専用パスワードを更新しました。全員の閲覧許可をリセットしました。", "success")

            elif action == "create_access_token":
                memo = (request.form.get("memo") or "").strip()
                if not memo:
                    flash("アクセストークンのメモは必須です。", "danger")
                elif len(memo) > 255:
                    flash("アクセストークンのメモは255文字以内で入力してください。", "danger")
                else:
                    created = create_payout_access_token(
                        db,
                        memo=memo,
                        issued_via="admin_ui",
                        issued_by_app=None,
                        created_by_admin=session.get("user") or "admin",
                    )
                    created["access_url"] = build_payout_access_url(created.get("token") or "")
                    _set_new_access_token_notice(created)
                    flash("アクセストークンを作成しました。", "success")

            elif action == "toggle_access_token":
                token_id = int(request.form.get("token_id") or 0)
                is_active = request.form.get("is_active") == "1"
                if not token_id:
                    flash("アクセストークンIDが不正です。", "danger")
                else:
                    toggle_payout_access_token_active(db, token_id, is_active)
                    flash(
                        "アクセストークンを有効化しました。" if is_active else "アクセストークンを無効化しました。",
                        "success",
                    )

            return redirect(url_for("bank_account.admin_payout"))

        settings = _get_settings(cursor)

        cursor.execute(
            """
            SELECT id,
                   paypay_send_id,
                   paypay_link,
                   is_active,
                   paypay_link_saved_at,
                   paypay_link_expired_notified_at,
                   updated_at
            FROM mfu_payout_paypay
            WHERE id = 1
            LIMIT 1
            """
        )
        paypay = cursor.fetchone() or {"id": 1, "is_active": 1}
        paypay_status = _build_paypay_link_status(paypay)

        cursor.execute(
            """
            SELECT id, label, bank_name, branch_name, account_number, is_active, sort_order, updated_at
            FROM mfu_payout_account
            ORDER BY sort_order ASC, id ASC
            """
        )
        accounts = cursor.fetchall()

        cursor.execute(
            """
            SELECT id,
                   token_prefix,
                   token_suffix,
                   memo,
                   is_active,
                   access_count,
                   last_accessed_at,
                   last_access_ip,
                   issued_via,
                   issued_by_app,
                   created_by_admin,
                   created_at,
                   updated_at,
                   expires_at
            FROM mfu_payout_access_token
            ORDER BY created_at DESC, id DESC
            """
        )
        access_tokens = cursor.fetchall()
        for token in access_tokens:
            token["token_preview"] = f"{token.get('token_prefix')}...{token.get('token_suffix')}"

        new_access_token = _consume_new_access_token_notice()
    finally:
        cursor.close()
        db.close()

    return render_template(
        "bank_account/admin.html",
        settings=settings,
        paypay=paypay,
        paypay_status=paypay_status,
        accounts=accounts,
        access_tokens=access_tokens,
        new_access_token=new_access_token,
    )
