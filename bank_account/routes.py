from __future__ import annotations

import time

import bcrypt
from flask import flash, redirect, render_template, request, session, url_for

from app import admin_required
from app.utils.db import get_db

from . import bank_account_bp
from .token_service import (
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



def _clear_payout_unlock_session(clear_failure: bool = False) -> None:
    session.pop("payout_unlocked", None)
    session.pop("payout_pwd_version", None)
    session.pop("payout_auth_method", None)
    session.pop("payout_token_id", None)
    if clear_failure:
        _clear_unlock_failure()



def _is_password_unlocked(current_version: int) -> bool:
    return (
        session.get("payout_unlocked") is True
        and session.get("payout_auth_method") in (None, "password")
        and session.get("payout_pwd_version") == current_version
    )



def _is_token_unlocked(db) -> bool:
    if session.get("payout_unlocked") is not True:
        return False
    if session.get("payout_auth_method") != "token":
        return False

    token_id = session.get("payout_token_id")
    if not token_id:
        return False

    token_row = get_payout_access_token_by_id(db, int(token_id))
    if not is_payout_access_token_usable(token_row):
        _clear_payout_unlock_session()
        return False
    return True



def _is_any_payout_unlocked(db, current_version: int) -> bool:
    return _is_password_unlocked(current_version) or _is_token_unlocked(db)



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
        "memo": created.get("memo"),
        "created_at": created.get("created_at").strftime("%Y-%m-%d %H:%M:%S") if created.get("created_at") else None,
    }


@bank_account_bp.route("/payout")
def payout_index():
    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        settings = _get_settings(cursor)
        version = int(settings.get("payout_password_version") or 1)

        if not _is_any_payout_unlocked(db, version):
            _clear_payout_unlock_session()
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
            SELECT paypay_send_id, paypay_link, is_active
            FROM mfu_payout_paypay
            WHERE id = 1
            LIMIT 1
            """
        )
        paypay = cursor.fetchone() or {}
    finally:
        cursor.close()
        db.close()

    return render_template(
        "bank_account/index.html",
        accounts=accounts,
        paypay=paypay,
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
            access_token = (request.form.get("access_token") or "").strip()

            if password:
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

            if access_token:
                token_row = verify_payout_access_token(db, access_token)
                if token_row:
                    session["payout_unlocked"] = True
                    session["payout_auth_method"] = "token"
                    session["payout_token_id"] = int(token_row["id"])
                    session.pop("payout_pwd_version", None)
                    touch_payout_access_token_usage(db, int(token_row["id"]), _client_ip())
                    _clear_unlock_failure()
                    flash("アクセストークンで振込先ページの閲覧を許可しました。", "success")
                    return redirect(url_for("bank_account.payout_index"))

                fail_count = int(session.get("payout_unlock_fail_count") or 0) + 1
                session["payout_unlock_fail_count"] = fail_count
                if fail_count >= MAX_UNLOCK_FAILURES:
                    session["payout_unlock_lock_until"] = now_ts + LOCK_SECONDS
                    flash("失敗が10回に達したため、10分間ロックしました。", "danger")
                else:
                    flash(f"アクセストークンが無効です。（{fail_count}/{MAX_UNLOCK_FAILURES}）", "danger")
                return redirect(url_for("bank_account.payout_unlock"))

            flash("パスワードまたはアクセストークンを入力してください。", "danger")
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
    _clear_payout_unlock_session(clear_failure=False)
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
                cursor.execute(
                    """
                    UPDATE mfu_payout_paypay
                    SET paypay_send_id = %s,
                        paypay_link = %s,
                        is_active = %s,
                        updated_at = NOW()
                    WHERE id = 1
                    """,
                    (send_id, link, is_active),
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
            SELECT id, paypay_send_id, paypay_link, is_active, updated_at
            FROM mfu_payout_paypay
            WHERE id = 1
            LIMIT 1
            """
        )
        paypay = cursor.fetchone() or {"id": 1, "is_active": 1}

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
        accounts=accounts,
        access_tokens=access_tokens,
        new_access_token=new_access_token,
    )
