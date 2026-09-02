# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from flask import request, render_template, jsonify, abort, url_for, redirect, session, current_app
from . import bp
from app.utils.db import get_db
from app.utils.admin_passkey_stepup import require_admin_passkey
from .utils import _require_mfu_login_redirect, _admin_csrf_token
from .utils import (
    _get_current_privacy_policy_config,
    _needs_privacy_policy_agreement,
    _privacy_policy_status,
    _privacy_policy_date_label,
)
from .deletion_service import anonymize_external_user

# users.py のトークン発行ユーティリティ（無い環境でも落ちないように）
try:
    from .users import _issue_verify_pin, _send_verify_pin_mail  # type: ignore
except Exception:
    _issue_verify_pin = None  # type: ignore
    _send_verify_pin_mail = None  # type: ignore


# ============= 内部ヘルパ =============
def _column_exists(table: str, column: str) -> bool:
    """information_schema で列の有無を確認（email_verified_at 非存在でも動かす）"""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT DATABASE()")
        dbname = (cur.fetchone() or [None])[0]
        if not dbname:
            return False
        cur.execute(
            """
            SELECT 1
              FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
             LIMIT 1
            """,
            (dbname, table, column),
        )
        return cur.fetchone() is not None
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass




def _format_datetime_jst(value) -> str | None:
    """日時を表示用の yyyy年mm月dd日 hh:mm:ss に整形する。"""
    if not value:
        return None

    dt = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = parsedate_to_datetime(raw)
        except Exception:
            dt = None
        if dt is None:
            # MySQL/ISO8601 文字列（末尾 Z を含む形式含む）を優先的に解釈
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except Exception:
                dt = None
        if dt is None:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except Exception:
                    continue

    if dt is None:
        return str(value)

    return dt.strftime("%Y年%m月%d日 %H:%M:%S")


def _format_email_verified_at_rows(rows: list[dict]) -> None:
    for r in rows:
        r["email_verified_at"] = _format_datetime_jst(r.get("email_verified_at"))

def _normalize_sort(sort_key: str | None, sort_order: str | None) -> tuple[str, str]:
    """一覧API向けのソートキーを正規化する。"""
    key = (sort_key or "id").strip().lower()
    if key not in ("id", "nickname"):
        key = "id"

    order = (sort_order or "asc").strip().lower()
    if order not in ("asc", "desc"):
        order = "asc"
    return key, order


def _normalize_deleted_filter(raw: str | None) -> str:
    mode = (raw or "active").strip().lower()
    if mode not in {"active", "deleted", "all"}:
        return "active"
    return mode


_PUSH_SUBSCRIPTION_COUNT_SQL = """
    (
      SELECT COUNT(*)
        FROM chat_push_subscriptions cps
       WHERE cps.actor_type = 'line'
         AND cps.actor_id = CAST(external_login_user.id AS CHAR)
    ) AS push_subscription_count
"""


# ============= 画面（一覧） =============
@bp.route("/admin/ext-users")
def admin_ext_users_index():
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    q = (request.args.get("q") or "").strip()
    sort_key, sort_order = _normalize_sort(request.args.get("sort"), request.args.get("order"))
    deleted_filter = _normalize_deleted_filter(request.args.get("deleted"))

    params: list = []
    where = []
    has_deleted = _column_exists("external_login_user", "is_deleted")
    if has_deleted:
        if deleted_filter == "active":
            where.append("COALESCE(is_deleted, 0)=0")
        elif deleted_filter == "deleted":
            where.append("COALESCE(is_deleted, 0)=1")
    if q:
        like = f"%{q}%"
        where.append(
            "(CAST(id AS CHAR) LIKE %s OR nickname LIKE %s OR x_id LIKE %s OR instagram_id LIKE %s OR email LIKE %s OR social_id LIKE %s)"
        )
        params += [like, like, like, like, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        has_verified = _column_exists("external_login_user", "email_verified_at")
        deleted_cols = "COALESCE(is_deleted, 0) AS is_deleted, deleted_at" if has_deleted else "0 AS is_deleted, NULL AS deleted_at"
        cols = f"""
            id,
            nickname,
            x_id,
            instagram_id,
            email,
            social_id,
            {deleted_cols},
            avatar_file,
            avatar_url,
            created_at,
            updated_at,
            admin_note,
            privacy_policy_agreed_at,
            privacy_policy_agreed_revised_date,
            COALESCE(chat_admin_alias, 0) AS chat_admin_alias,
            COALESCE(notify_album_upload, 1)  AS notify_album_upload,
            COALESCE(notify_album_process, 1) AS notify_album_process,
            {_PUSH_SUBSCRIPTION_COUNT_SQL},
            (
              SELECT COUNT(*)
                FROM external_login_user_card_data c
               WHERE c.user_id = external_login_user.id
                 AND c.deleted_at IS NULL
            ) AS card_count
        """
        if has_verified:
            cols += ", email_verified_at"

        order_by = f"id {sort_order}" if sort_key == "id" else f"nickname {sort_order}, id {sort_order}"
        cur.execute(f"""
            SELECT {cols}
              FROM external_login_user
              {sql_where}
             ORDER BY {order_by}
        """, params)
        initial_items = cur.fetchall() or []
        if not has_verified:
            for r in initial_items:
                r["email_verified_at"] = None
        _format_email_verified_at_rows(initial_items)
        current_privacy_config = _get_current_privacy_policy_config()
        for r in initial_items:
            r["privacy_policy_status"] = _privacy_policy_status(r, current_privacy_config)
            r["privacy_policy_agreed_revised_date_label"] = _privacy_policy_date_label(r.get("privacy_policy_agreed_revised_date"))
            r["privacy_policy_agreed_at_label"] = _format_datetime_jst(r.get("privacy_policy_agreed_at"))

        cur.execute(f"SELECT COUNT(*) AS c FROM external_login_user {sql_where}", params)
        total = int((cur.fetchone() or {}).get("c", 0))
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass

    return render_template(
        "admin_ext_users.html",
        admin_csrf=_admin_csrf_token(),
        initial_items=initial_items,
        initial_total=total,
        initial_per_page=total,
        initial_page=1,
        initial_q=q,
        initial_sort=sort_key,
        initial_order=sort_order,
        initial_deleted=deleted_filter,
        current_privacy_config=current_privacy_config,
    )


# ============= API（一括） =============
@bp.get("/admin/ext-users/data")
def admin_ext_users_data():
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    q = (request.args.get("q") or "").strip()
    sort_key, sort_order = _normalize_sort(request.args.get("sort"), request.args.get("order"))
    deleted_filter = _normalize_deleted_filter(request.args.get("deleted"))
    try:
        page = max(int(request.args.get("page") or 1), 1)
    except Exception:
        page = 1
    try:
        per_page = min(max(int(request.args.get("per_page") or 1000), 1), 10000)
    except Exception:
        per_page = 1000

    params: list = []
    where = []
    has_deleted = _column_exists("external_login_user", "is_deleted")
    if has_deleted:
        if deleted_filter == "active":
            where.append("COALESCE(is_deleted, 0)=0")
        elif deleted_filter == "deleted":
            where.append("COALESCE(is_deleted, 0)=1")
    if q:
        like = f"%{q}%"
        where.append(
            "(CAST(id AS CHAR) LIKE %s OR nickname LIKE %s OR x_id LIKE %s OR instagram_id LIKE %s OR email LIKE %s OR social_id LIKE %s)"
        )
        params += [like, like, like, like, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""

    has_verified = _column_exists("external_login_user", "email_verified_at")
    deleted_cols = "COALESCE(is_deleted, 0) AS is_deleted, deleted_at" if has_deleted else "0 AS is_deleted, NULL AS deleted_at"
    cols = f"""
        id, nickname, x_id, instagram_id, email, social_id,
        {deleted_cols},
        avatar_file, avatar_url, created_at, updated_at,
        admin_note,
        privacy_policy_agreed_at,
        privacy_policy_agreed_revised_date,
        COALESCE(chat_admin_alias, 0) AS chat_admin_alias,
        COALESCE(notify_album_upload, 1)  AS notify_album_upload,
        COALESCE(notify_album_process, 1) AS notify_album_process,
        {_PUSH_SUBSCRIPTION_COUNT_SQL},
        (
          SELECT COUNT(*)
            FROM external_login_user_card_data c
           WHERE c.user_id = external_login_user.id
             AND c.deleted_at IS NULL
        ) AS card_count
    """
    if has_verified:
        cols += ", email_verified_at"

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute(f"SELECT COUNT(*) AS c FROM external_login_user {sql_where}", params)
        total = int(cur.fetchone()["c"])
        offset = (page - 1) * per_page

        order_by = f"id {sort_order}" if sort_key == "id" else f"nickname {sort_order}, id {sort_order}"
        cur.execute(f"""
            SELECT {cols}
              FROM external_login_user
              {sql_where}
             ORDER BY {order_by}
             LIMIT %s OFFSET %s
        """, params + [per_page, offset])
        rows = cur.fetchall() or []
        if not has_verified:
            for r in rows:
                r["email_verified_at"] = None
        _format_email_verified_at_rows(rows)
        current_privacy_config = _get_current_privacy_policy_config()
        for r in rows:
            r["privacy_policy_status"] = _privacy_policy_status(r, current_privacy_config)
            r["privacy_policy_agreed_revised_date_label"] = _privacy_policy_date_label(r.get("privacy_policy_agreed_revised_date"))
            r["privacy_policy_agreed_at_label"] = _format_datetime_jst(r.get("privacy_policy_agreed_at"))
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return jsonify({
        "ok": True,
        "items": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "sort": sort_key,
        "order": sort_order,
        "deleted": deleted_filter,
        "q": q,
    })

# ============= API（単票：旧モーダル用） =============
@bp.get("/admin/ext-users/<int:user_id>/detail")
def admin_ext_users_detail(user_id: int):
    """（旧モーダル用）単票取得。"""
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        has_deleted = _column_exists("external_login_user", "is_deleted")
        deleted_cols = (
            "COALESCE(is_deleted, 0) AS is_deleted, deleted_at, deleted_by, deletion_reason,"
            if has_deleted else
            "0 AS is_deleted, NULL AS deleted_at, NULL AS deleted_by, NULL AS deletion_reason,"
        )
        cur.execute(f"""
            SELECT
              id,
              nickname,
              x_id,
              instagram_id,
              email,
              social_id,
              {deleted_cols}
              avatar_file,
              avatar_url,
              created_at,
              updated_at,
              admin_note,
              COALESCE(chat_admin_alias, 0) AS chat_admin_alias,
              COALESCE(notify_album_upload, 1)  AS notify_album_upload,
              COALESCE(notify_album_process, 1) AS notify_album_process,
              {_PUSH_SUBSCRIPTION_COUNT_SQL}
            FROM external_login_user
            WHERE id=%s
            LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            abort(404)
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass

    return jsonify({"ok": True, "item": row})


# ============= 更新 =============
@bp.post("/admin/ext-users/<int:user_id>/update")
def admin_ext_users_update(user_id: int):
    """項目更新（nickname / x_id / instagram_id / email / admin_note）
       フォームPOST: 成功時は一覧へリダイレクト（?saved=1）
       JSON(AJAX): 互換のため JSON を返す
    """
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    token_req = (request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or "").strip()
    if not token_req or token_req != _admin_csrf_token():
        if request.is_json:
            return jsonify({"ok": False, "error": "invalid_csrf"}), 400
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="csrf"))

    data = request.get_json(silent=True) or request.form
    nickname   = (data.get("nickname") or "").strip()
    x_id_raw   = (data.get("x_id") or "").strip().lstrip("@")
    ig_raw     = (data.get("instagram_id") or "").strip().lstrip("@")
    email      = (data.get("email") or "").strip().lower() or None
    admin_note = (data.get("admin_note") or "").strip() or None  # ★追加: 内部メモ
    chat_admin_alias = "1" if str(data.get("chat_admin_alias") or "").strip().lower() in {"1", "true", "on", "yes"} else "0"

    errors = {}
    if not nickname:
        errors["nickname"] = "ニックネームは必須です。"
    if x_id_raw and not re.fullmatch(r"[A-Za-z0-9_]{1,15}", x_id_raw):
        errors["x_id"] = "X IDは @なし、半角英数と_で1〜15文字。"
    if ig_raw:
        ok = re.fullmatch(r"[A-Za-z0-9._]{1,30}", ig_raw) and not ig_raw.startswith(".") and not ig_raw.endswith(".") and ".." not in ig_raw
        if not ok:
            errors["instagram_id"] = "Instagram IDは英数・.・_で1〜30文字。先頭/末尾の . と .. は不可。"

    if errors:
        if request.is_json:
            return jsonify({"ok": False, "errors": errors}), 400
        # フォームの場合は編集ページに戻してエラー表示（簡易）
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="validate"))

    has_verified = _column_exists("external_login_user", "email_verified_at")

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT email, COALESCE(is_deleted, 0) AS is_deleted FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
        row = cur.fetchone()
        if not row:
            if request.is_json:
                return jsonify({"ok": False, "error": "not_found"}), 404
            return redirect(url_for("external_login_user.admin_ext_users_index"))

        if int((row[1] if isinstance(row, tuple) else row.get("is_deleted", 0)) or 0) == 1:
            if request.is_json:
                return jsonify({"ok": False, "error": "deleted_user"}), 409
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="deleted"))
        email_before = (row[0] if isinstance(row, tuple) else row.get("email")) if row else None
        email_changed = (email or None) != (email_before or None)

        if email:
            cur.execute(
                """SELECT id FROM external_login_user
                     WHERE LOWER(TRIM(email))=%s AND id<>%s LIMIT 1""",
                (email, user_id),
            )
            if cur.fetchone():
                if request.is_json:
                    return jsonify({"ok": False, "errors": {"email": "このメールアドレスは既に登録されています。"}}), 409
                return redirect(url_for(
                    "external_login_user.admin_ext_users_edit_page",
                    user_id=user_id,
                    error="email_duplicate",
                ))

        if email_changed and has_verified:
            cur.execute("""
                UPDATE external_login_user
                   SET nickname=%s,
                       x_id=%s,
                       instagram_id=%s,
                       email=%s,
                       admin_note=%s,
                       chat_admin_alias=%s,
                       email_verified_at=NULL
                 WHERE id=%s
                 LIMIT 1
            """, (nickname, x_id_raw or None, ig_raw or None, email, admin_note, chat_admin_alias, user_id))
        else:
            cur.execute("""
                UPDATE external_login_user
                   SET nickname=%s,
                       x_id=%s,
                       instagram_id=%s,
                       email=%s,
                       admin_note=%s,
                       chat_admin_alias=%s
                 WHERE id=%s
                 LIMIT 1
            """, (nickname, x_id_raw or None, ig_raw or None, email, admin_note, chat_admin_alias, user_id))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # 成功時の応答
    if request.is_json:
        return jsonify({"ok": True})
    # フォームは一覧へ
    return redirect(url_for("external_login_user.admin_ext_users_index", saved=1))


@bp.post("/admin/ext-users/<int:user_id>/anonymize-delete")
def admin_ext_users_anonymize_delete(user_id: int):
    guard = _require_mfu_login_redirect()
    if guard:
        return guard
    guard = require_admin_passkey(f"external_user_anonymize:{user_id}")
    if guard:
        return guard

    token_req = (request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or "").strip()
    if not token_req or token_req != _admin_csrf_token():
        if request.is_json:
            return jsonify({"ok": False, "error": "invalid_csrf"}), 400
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="csrf"))

    reason = (request.form.get("reason") or (request.get_json(silent=True) or {}).get("reason") or "").strip() or None
    executed_by = (session.get("user") or "admin")
    try:
        result = anonymize_external_user(
            user_id=int(user_id),
            executed_by=str(executed_by)[:80],
            reason=(reason[:255] if reason else None),
        )
    except ValueError:
        if request.is_json:
            return jsonify({"ok": False, "error": "not_found"}), 404
        return redirect(url_for("external_login_user.admin_ext_users_index", error="not_found"))
    except Exception:
        current_app.logger.exception("admin anonymize delete failed user_id=%s", user_id)
        if request.is_json:
            return jsonify({"ok": False, "error": "internal_error"}), 500
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="anonymize"))

    if request.is_json:
        return jsonify(result)
    return redirect(
        url_for(
            "external_login_user.admin_ext_users_edit_page",
            user_id=user_id,
            anonymized="1",
            already_deleted=("1" if result.get("already_deleted") else "0"),
        )
    )


# ============= 再送（送信） =============
@bp.post("/admin/ext-users/<int:user_id>/resend-verify")
def admin_ext_users_resend_verify(user_id: int):
    """
    確認コードの再送
    - Form投稿: 成功時は編集ページへリダイレクト（?sent=1）
    - JSON投稿(AJAX): 互換のため JSON を返す
    """
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    token_req = (request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or "").strip()
    if not token_req or token_req != _admin_csrf_token():
        if request.is_json:
            return jsonify({"ok": False, "error": "invalid_csrf"}), 400
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="csrf"))

    if not _issue_verify_pin or not _send_verify_pin_mail:
        if request.is_json:
            return jsonify({"ok": False, "error": "pin_issuer_unavailable"}), 500
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="pin"))

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT email, COALESCE(is_deleted, 0) AS is_deleted FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
        row = cur.fetchone()
        if not row:
            if request.is_json:
                return jsonify({"ok": False, "error": "not_found"}), 404
            return redirect(url_for("external_login_user.admin_ext_users_index"))

        if int((row[1] if isinstance(row, tuple) else row.get("is_deleted", 0)) or 0) == 1:
            if request.is_json:
                return jsonify({"ok": False, "error": "deleted_user"}), 409
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="deleted"))
        email = row[0] if isinstance(row, tuple) else row.get("email")
        if not email:
            if request.is_json:
                return jsonify({"ok": False, "error": "no_email"}), 400
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="no_email"))

        ok_pin, reason, pin_raw = _issue_verify_pin(user_id, email)  # type: ignore
        if not ok_pin or not pin_raw:
            if request.is_json:
                return jsonify({"ok": False, "error": reason}), 429 if reason in ("cooldown", "rate_limited") else 400
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error=reason))

        try:
            _send_verify_pin_mail(email, pin_raw)  # type: ignore
        except Exception:
            if request.is_json:
                return jsonify({"ok": False, "error": "smtp_error"}), 502
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="smtp"))

        if request.is_json:
            return jsonify({"ok": True})
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, sent="1"))
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass


# ============= 再送の確認ページ（GET） =============
@bp.get("/admin/ext-users/<int:user_id>/resend-confirm", endpoint="admin_ext_users_resend_confirm")
def admin_ext_users_resend_confirm(user_id: int):
    """再送のサーバー確認ページ（JS不要）"""
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute(
            """
            SELECT
              id,
              email,
              COALESCE(is_deleted, 0) AS is_deleted
            FROM external_login_user
            WHERE id=%s
            LIMIT 1
            """,
            (user_id,),
        )
        u = cur.fetchone()
        if not u:
            abort(404)
        if int(u.get("is_deleted") or 0) == 1:
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="deleted"))
        if not u.get("email"):
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="no_email"))
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass

    return render_template("admin_ext_users_resend_confirm.html", u=u, admin_csrf=_admin_csrf_token())


# ============= 別ページ編集（GET） =============
@bp.get("/admin/ext-users/<int:user_id>/edit", endpoint="admin_ext_users_edit_page")
def admin_ext_users_edit_page(user_id: int):
    """外部ログインユーザー 編集ページ（別ページ実装）"""
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        has_verified = _column_exists("external_login_user", "email_verified_at")
        has_deleted = _column_exists("external_login_user", "is_deleted")
        deleted_cols = (
            "COALESCE(is_deleted, 0) AS is_deleted, deleted_at, deleted_by, deletion_reason,"
            if has_deleted else
            "0 AS is_deleted, NULL AS deleted_at, NULL AS deleted_by, NULL AS deletion_reason,"
        )
        cols = f"""
            id, nickname, x_id, instagram_id, email, social_id,
            {deleted_cols}
            avatar_file, avatar_url, created_at, updated_at,
            admin_note,
            privacy_policy_agreed_at,
            privacy_policy_agreed_revised_date,
            COALESCE(chat_admin_alias, 0) AS chat_admin_alias,
            COALESCE(notify_album_upload, 1)  AS notify_album_upload,
            COALESCE(notify_album_process, 1) AS notify_album_process,
            {_PUSH_SUBSCRIPTION_COUNT_SQL}
        """
        if has_verified:
            cols += ", email_verified_at"
        cur.execute(f"SELECT {cols} FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
        user = cur.fetchone()
        if not user:
            abort(404)
        if not has_verified:
            user["email_verified_at"] = None

        # 参加中イベント一覧
        cur.execute(
            """
            SELECT
              m.id AS member_id,
              e.id AS event_id,
              e.title,
              e.starts_at,
              m.status,
              m.payment_status,
              COALESCE(m.process,0) AS process,
              COALESCE(m.is_canceled,0) AS is_canceled,
              m.canceled_at,
              m.canceled_by
            FROM mfu_event_member AS m
            JOIN mfu_event AS e ON e.id = m.event_id
            WHERE m.user_id=%s
            ORDER BY COALESCE(e.starts_at, '9999-12-31') DESC, e.id DESC
            """,
            (user_id,),
        )
        memberships = cur.fetchall() or []

        # 参加可能イベント（未参加のもの）
        cur.execute(
            """
            SELECT e.id, e.title, e.starts_at
              FROM mfu_event AS e
             WHERE e.id NOT IN (
                    SELECT event_id FROM mfu_event_member WHERE user_id=%s
                   )
             ORDER BY COALESCE(e.starts_at, '9999-12-31') ASC, e.id ASC
             LIMIT 200
            """,
            (user_id,),
        )
        assignable = cur.fetchall() or []
        can_assign_events = int(user.get("is_deleted") or 0) != 1
        if not can_assign_events:
            assignable = []

    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass

    current_privacy_config = _get_current_privacy_policy_config()
    user["privacy_policy_status"] = _privacy_policy_status(user, current_privacy_config)
    user["privacy_policy_agreed_revised_date_label"] = _privacy_policy_date_label(user.get("privacy_policy_agreed_revised_date"))
    user["privacy_policy_agreed_at_label"] = _format_datetime_jst(user.get("privacy_policy_agreed_at"))
    user["privacy_policy_current_revised_date_label"] = _privacy_policy_date_label(current_privacy_config.get("privacy_policy_revised_date"))
    user["privacy_policy_needs_reagreement"] = _needs_privacy_policy_agreement(user, current_privacy_config)

    return render_template(
        "admin_ext_users_edit.html",
        u=user,
        memberships=memberships,
        assignable_events=assignable,
        can_assign_events=can_assign_events,
        admin_csrf=_admin_csrf_token(),
        current_privacy_config=current_privacy_config,
    )


# ============= 参加追加（POST） =============
@bp.post("/admin/ext-users/<int:user_id>/assign")
def admin_ext_users_assign(user_id: int):
    """
    指定ユーザーをイベントに参加追加する
    - Form POST（csrf必須）
    - 既に参加済みなら duplicated=1 を付けて編集ページに戻す
    """
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    token_req = (request.form.get("csrf_token") or "").strip()
    if not token_req or token_req != _admin_csrf_token():
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="csrf"))

    try:
        event_id = int(request.form.get("event_id") or "0")
    except Exception:
        event_id = 0
    if event_id <= 0:
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="bad_event"))

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute(
            """
            SELECT id, COALESCE(is_deleted,0) AS is_deleted
              FROM external_login_user
             WHERE id=%s
             LIMIT 1
            """,
            (user_id,),
        )
        user_row = cur.fetchone()
        if not user_row:
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="not_found"))
        is_deleted = user_row[1] if isinstance(user_row, tuple) else user_row.get("is_deleted", 0)
        if int(is_deleted or 0) == 1:
            current_app.logger.warning("deleted external user assign blocked: user_id=%s event_id=%s", user_id, event_id)
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="deleted"))

        # すでに参加済みか軽く確認（UNIQUE制約もあるが事前チェックでメッセージを素直に）
        cur.execute("SELECT id, COALESCE(is_canceled,0) AS is_canceled FROM mfu_event_member WHERE event_id=%s AND user_id=%s", (event_id, user_id))
        existing = cur.fetchone()
        if existing:
            existing_id = existing[0] if isinstance(existing, tuple) else existing.get("id")
            existing_canceled = existing[1] if isinstance(existing, tuple) else existing.get("is_canceled", 0)
            if int(existing_canceled or 0) == 1:
                cur.execute("""
                    UPDATE mfu_event_member
                       SET is_canceled=0,
                           canceled_at=NULL,
                           canceled_by=NULL
                     WHERE id=%s
                     LIMIT 1
                """, (existing_id,))
                db.commit()
                return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, revived=1))
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, duplicated=1))

        # 追加：初期は pending / unpaid で登録
        cur.execute(
            """
            INSERT INTO mfu_event_member (event_id, user_id, status, payment_status)
            VALUES (%s, %s, 'pending', 'unpaid')
            """,
            (event_id, user_id),
        )
        db.commit()
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass

    return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, assigned=1))


@bp.post("/admin/ext-users/<int:user_id>/membership/<int:member_id>/process",
         endpoint="admin_ext_users_member_process_update")
def admin_ext_users_member_process_update(user_id: int, member_id: int):
    """
    管理画面：外部ユーザー詳細ページから、イベント参加レコードごとの加工回し必要フラグを更新する。
    process=1: 加工回し必要
    process=0: 加工回し不要
    """
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    token_req = (request.form.get("csrf_token") or "").strip()
    if not token_req or token_req != _admin_csrf_token():
        return redirect(url_for(
            "external_login_user.admin_ext_users_edit_page",
            user_id=user_id,
            error="csrf",
        ))

    process_flag = 1 if (request.form.get("process") or "").strip().lower() in {"1", "on", "true", "yes"} else 0

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT
              m.id,
              m.user_id,
              m.event_id,
              COALESCE(m.is_canceled,0) AS is_canceled
            FROM mfu_event_member AS m
            JOIN external_login_user AS u ON u.id = m.user_id
            WHERE m.id=%s
              AND m.user_id=%s
            LIMIT 1
        """, (member_id, user_id))
        row = cur.fetchone()

        if not row:
            return redirect(url_for(
                "external_login_user.admin_ext_users_edit_page",
                user_id=user_id,
                error="not_found",
            ))

        if int(row.get("is_canceled") or 0) == 1:
            return redirect(url_for(
                "external_login_user.admin_ext_users_edit_page",
                user_id=user_id,
                error="canceled",
            ))

        cur.execute("""
            UPDATE mfu_event_member
               SET process=%s
             WHERE id=%s
               AND user_id=%s
             LIMIT 1
        """, (process_flag, member_id, user_id))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        current_app.logger.exception(
            "admin ext user membership process update failed: user_id=%s member_id=%s process=%s",
            user_id,
            member_id,
            process_flag,
        )
        return redirect(url_for(
            "external_login_user.admin_ext_users_edit_page",
            user_id=user_id,
            error="process_update_failed",
        ))
    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass

    return redirect(url_for(
        "external_login_user.admin_ext_users_edit_page",
        user_id=user_id,
        process_saved="1",
    ))

# ============= 参加削除 確認ページ（GET） =============
@bp.get("/admin/ext-users/<int:user_id>/membership/<int:member_id>/delete-confirm",
        endpoint="admin_ext_users_member_delete_confirm")
def admin_ext_users_member_delete_confirm(user_id: int, member_id: int):
    """ユーザーのイベント参加レコードをキャンセルする前の確認ページ"""
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        # 該当の membership がユーザーのものか検証して情報取得
        cur.execute("""
            SELECT m.id AS member_id, m.user_id, m.event_id, m.status, m.payment_status,
                   COALESCE(m.is_canceled,0) AS is_canceled, m.canceled_at, m.canceled_by,
                   e.title, e.starts_at
              FROM mfu_event_member AS m
              JOIN mfu_event AS e ON e.id = m.event_id
             WHERE m.id=%s AND m.user_id=%s
             LIMIT 1
        """, (member_id, user_id))
        m = cur.fetchone()
        if not m:
            # 見つからない or 他人のデータ
            return redirect(url_for("external_login_user.admin_ext_users_edit_page",
                                    user_id=user_id, error="not_found"))
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return render_template("admin_ext_users_member_delete_confirm.html",
                           u_id=user_id, m=m, admin_csrf=_admin_csrf_token())


# ============= 参加削除 実行（POST） =============
@bp.post("/admin/ext-users/<int:user_id>/membership/<int:member_id>/delete",
         endpoint="admin_ext_users_member_delete")
def admin_ext_users_member_delete(user_id: int, member_id: int):
    """ユーザーのイベント参加レコードをキャンセル済みにする"""
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    # CSRF
    token_req = (request.form.get("csrf_token") or "").strip()
    if not token_req or token_req != _admin_csrf_token():
        return redirect(url_for("external_login_user.admin_ext_users_edit_page",
                                user_id=user_id, error="csrf"))

    db = get_db(); cur = db.cursor()
    try:
        # 所有確認してからキャンセル
        cur.execute("SELECT 1 FROM mfu_event_member WHERE id=%s AND user_id=%s LIMIT 1",
                    (member_id, user_id))
        if not cur.fetchone():
            return redirect(url_for("external_login_user.admin_ext_users_edit_page",
                                    user_id=user_id, error="not_found"))

        canceled_by = (session.get("user") or "admin")
        cur.execute("""
            UPDATE mfu_event_member
               SET is_canceled=1,
                   canceled_at=NOW(),
                   canceled_by=%s
             WHERE id=%s
             LIMIT 1
        """, (canceled_by, member_id))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return redirect(url_for("external_login_user.admin_ext_users_edit_page",
                            user_id=user_id, canceled="1"))
