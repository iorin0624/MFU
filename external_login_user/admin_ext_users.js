# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from flask import request, render_template, jsonify, abort, url_for
from . import bp
from app.utils.db import get_db
from .utils import _require_mfu_login_redirect, _admin_csrf_token
from app.utils.mail import send_mail  # ← 指定の mail.py を利用

# users.py のトークン発行ユーティリティ（無い環境でも落ちないように）
try:
    from .users import _issue_email_verify_token  # type: ignore
except Exception:
    _issue_email_verify_token = None  # type: ignore


# ============= 内部ヘルパ =============
def _column_exists(table: str, column: str) -> bool:
    """information_schema で列の有無を確認（email_verified_at 非存在でも動かす）"""
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT DATABASE()")
        dbname = (cur.fetchone() or [None])[0]
        if not dbname:
            return False
        cur.execute("""
            SELECT 1
              FROM information_schema.COLUMNS
             WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
             LIMIT 1
        """, (dbname, table, column))
        return cur.fetchone() is not None
    finally:
        try: cur.close(); db.close()
        except Exception: pass


# ============= 画面（一覧） =============
@bp.route("/admin/ext-users")
def admin_ext_users_index():
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        has_verified = _column_exists("external_login_user", "email_verified_at")
        cols = """
            id, nickname, x_id, instagram_id, email, social_id,
            avatar_file, avatar_url, created_at, updated_at
        """
        if has_verified:
            cols += ", email_verified_at"
        cur.execute(f"""
            SELECT {cols}
              FROM external_login_user
             ORDER BY id DESC
             LIMIT 50
        """)
        initial_items = cur.fetchall() or []
        if not has_verified:
            for r in initial_items:
                r["email_verified_at"] = None

        cur.execute("SELECT COUNT(*) AS c FROM external_login_user")
        total = int((cur.fetchone() or {}).get("c", 0))
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return render_template(
        "admin_ext_users.html",
        admin_csrf=_admin_csrf_token(),
        initial_items=initial_items,
        initial_total=total,
        initial_per_page=50,
        initial_page=1,
    )


# ============= API（一括） =============
@bp.get("/admin/ext-users/data")
def admin_ext_users_data():
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    q = (request.args.get("q") or "").strip()
    try:
        page = max(int(request.args.get("page") or 1), 1)
    except Exception:
        page = 1
    try:
        per_page = min(max(int(request.args.get("per_page") or 50), 1), 200)
    except Exception:
        per_page = 50

    params: list = []
    where = []
    if q:
        like = f"%{q}%"
        where.append("(nickname LIKE %s OR x_id LIKE %s OR instagram_id LIKE %s OR email LIKE %s OR social_id LIKE %s)")
        params += [like, like, like, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""

    has_verified = _column_exists("external_login_user", "email_verified_at")
    cols = """
        id, nickname, x_id, instagram_id, email, social_id,
        avatar_file, avatar_url, created_at, updated_at
    """
    if has_verified:
        cols += ", email_verified_at"

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute(f"SELECT COUNT(*) AS c FROM external_login_user {sql_where}", params)
        total = int(cur.fetchone()["c"])
        offset = (page - 1) * per_page

        cur.execute(f"""
            SELECT {cols}
              FROM external_login_user
              {sql_where}
             ORDER BY id DESC
             LIMIT %s OFFSET %s
        """, params + [per_page, offset])
        rows = cur.fetchall() or []
        if not has_verified:
            for r in rows:
                r["email_verified_at"] = None
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return jsonify({"ok": True, "items": rows, "total": total, "page": page, "per_page": per_page})


@bp.get("/admin/ext-users/<int:user_id>/detail")
def admin_ext_users_detail(user_id: int):
    """（旧モーダル用）単票取得。"""
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT
              id, nickname, x_id, instagram_id, email, social_id,
              avatar_file, avatar_url, created_at, updated_at
            FROM external_login_user
            WHERE id=%s
            LIMIT 1
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            abort(404)
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return jsonify({"ok": True, "item": row})


@bp.post("/admin/ext-users/<int:user_id>/update")
def admin_ext_users_update(user_id: int):
    """
    項目更新（nickname / x_id / instagram_id / email）
    Body: JSON or form
    """
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    token_req = (request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or "").strip()
    if not token_req or token_req != _admin_csrf_token():
        return jsonify({"ok": False, "error": "invalid_csrf"}), 400

    data = request.get_json(silent=True) or request.form
    nickname = (data.get("nickname") or "").strip()
    x_id_raw = (data.get("x_id") or "").strip().lstrip("@")
    ig_raw   = (data.get("instagram_id") or "").strip().lstrip("@")
    email    = (data.get("email") or "").strip() or None

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
        return jsonify({"ok": False, "errors": errors}), 400

    has_verified = _column_exists("external_login_user", "email_verified_at")

    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT email FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not_found"}), 404
        email_before = (row[0] if isinstance(row, tuple) else row.get("email")) if row else None
        email_changed = (email or None) != (email_before or None)

        if email_changed and has_verified:
            cur.execute("""
                UPDATE external_login_user
                   SET nickname=%s, x_id=%s, instagram_id=%s, email=%s,
                       email_verified_at=NULL
                 WHERE id=%s
                 LIMIT 1
            """, (nickname, x_id_raw or None, ig_raw or None, email, user_id))
        else:
            cur.execute("""
                UPDATE external_login_user
                   SET nickname=%s, x_id=%s, instagram_id=%s, email=%s
                 WHERE id=%s
                 LIMIT 1
            """, (nickname, x_id_raw or None, ig_raw or None, email, user_id))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return jsonify({"ok": True})


@bp.post("/admin/ext-users/<int:user_id>/resend-verify")
def admin_ext_users_resend_verify(user_id: int):
    """
    メール確認リンクの再送
    - トークンを発行（users._issue_email_verify_token）
    - mail.py の send_mail() で送信
    """
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    token_req = (request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or "").strip()
    if not token_req or token_req != _admin_csrf_token():
        return jsonify({"ok": False, "error": "invalid_csrf"}), 400

    if not _issue_email_verify_token:
        return jsonify({"ok": False, "error": "token_issuer_unavailable"}), 500

    # 対象メール取得
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT email FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"ok": False, "error": "not_found"}), 404

        email = row[0] if isinstance(row, tuple) else row.get("email")
        if not email:
            return jsonify({"ok": False, "error": "no_email"}), 400

        # トークン発行
        token_raw = _issue_email_verify_token(user_id, email)  # type: ignore

        # 検証URL
        verify_url_get = url_for("external_login_user.email_verify", _external=True) + f"?t={token_raw}"

        subject = "イベント管理システムからメールアドレス確認のお願い"
        body = (
            "メールアドレスの確認をお願いします。\n"
            "下記の確認ページを開き、「確認する」ボタンを押してください（有効期限: 24時間）。\n\n"
            f"{verify_url_get}\n\n"
            "—\n"
            "発行元：MFU イベント管理\n"
            "このメールに心当たりが無い場合は破棄してください。"
        )

        # ここで mail.py の send_mail を使用（ホスト/ポートは mail.py のデフォルト: localhost:25）
        try:
            send_mail(
                to=email,
                subject=subject,
                body=body,
                event_uuid=None,   # From: noreply@mail.iori0624.jp
                # 必要なら smtp_host / smtp_port / timeout をここで明示
                # smtp_host="localhost", smtp_port=25, timeout=10,
            )
        except Exception as e:
            # SMTP接続や拒否などの例外を呼び元に返す
            return jsonify({"ok": False, "error": "smtp_error", "detail": str(e)}), 502

        return jsonify({"ok": True})
    finally:
        try: cur.close(); db.close()
        except Exception: pass


# ============= 別ページ編集 =============
@bp.get("/admin/ext-users/<int:user_id>/edit")
def admin_ext_users_edit_page(user_id: int):
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        has_verified = _column_exists("external_login_user", "email_verified_at")
        cols = """
            id, nickname, x_id, instagram_id, email, social_id,
            avatar_file, avatar_url, created_at, updated_at
        """
        if has_verified:
            cols += ", email_verified_at"
        cur.execute(f"SELECT {cols} FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
        user = cur.fetchone()
        if not user:
            abort(404)
        if not has_verified:
            user["email_verified_at"] = None
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return render_template("admin_ext_users_edit.html", u=user, admin_csrf=_admin_csrf_token())
