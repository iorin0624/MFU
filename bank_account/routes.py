from __future__ import annotations

import time

import bcrypt
from flask import flash, redirect, render_template, request, session, url_for

from app import admin_required
from app.utils.db import get_db

from . import bank_account_bp

MAX_UNLOCK_FAILURES = 10
LOCK_SECONDS = 10 * 60


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


def _is_unlocked(current_version: int) -> bool:
    return (
        session.get("payout_unlocked") is True
        and session.get("payout_pwd_version") == current_version
    )


@bank_account_bp.route("/payout")
def payout_index():
    db = get_db()
    cursor = db.cursor(dictionary=True)
    try:
        settings = _get_settings(cursor)
        version = int(settings.get("payout_password_version") or 1)

        if not settings.get("payout_password_hash"):
            return redirect(url_for("bank_account.payout_unlock", setup_required=1))

        if not _is_unlocked(version):
            session.pop("payout_unlocked", None)
            session.pop("payout_pwd_version", None)
            return redirect(url_for("bank_account.payout_unlock"))

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

            if not pwd_hash:
                flash("管理者がまだパスワードを設定していません。", "warning")
                return redirect(url_for("bank_account.payout_unlock"))

            submitted = (request.form.get("password") or "").encode("utf-8")
            stored = pwd_hash.encode("utf-8")
            if submitted and bcrypt.checkpw(submitted, stored):
                session["payout_unlocked"] = True
                session["payout_pwd_version"] = version
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
    session.pop("payout_unlocked", None)
    session.pop("payout_pwd_version", None)
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
    finally:
        cursor.close()
        db.close()

    return render_template(
        "bank_account/admin.html",
        settings=settings,
        paypay=paypay,
        accounts=accounts,
    )
