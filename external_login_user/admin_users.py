# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from flask import request, render_template, jsonify, abort, url_for, redirect
from . import bp
from app.utils.db import get_db
from .utils import _require_mfu_login_redirect, _admin_csrf_token
from app.utils.mail import send_mail  # /mnt/mfu/app/utils/mail.py

# users.py のトークン発行ユーティリティ（無い環境でも落ちないように）
try:
    from .users import _issue_email_verify_token  # type: ignore
except Exception:
    _issue_email_verify_token = None  # type: ignore


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


# ============= 画面（一覧） =============
@bp.route("/admin/ext-users")
def admin_ext_users_index():
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        has_verified = _column_exists("external_login_user", "email_verified_at")
        cols = """
            id,
            nickname,
            x_id,
            instagram_id,
            email,
            social_id,
            avatar_file,
            avatar_url,
            created_at,
            updated_at,
            admin_note,
            COALESCE(notify_album_upload, 1)  AS notify_album_upload,
            COALESCE(notify_album_process, 1) AS notify_album_process,
            (
              SELECT COUNT(*)
                FROM external_login_user_card_data c
               WHERE c.user_id = external_login_user.id
                 AND c.deleted_at IS NULL
            ) AS card_count
        """
        if has_verified:
            cols += ", email_verified_at"

        # ★ 並び順: 先頭が英数 → かな → その他（漢字など） → その中で nickname 昇順
        cur.execute(f"""
            SELECT {cols}
              FROM external_login_user
             ORDER BY
               CASE
                 WHEN nickname REGEXP '^[0-9A-Za-z]' THEN 0
                 WHEN nickname REGEXP '^[ぁ-ゟァ-ヿー]' THEN 1
                 ELSE 2
               END,
               nickname
             LIMIT 50
        """)
        initial_items = cur.fetchall() or []
        if not has_verified:
            for r in initial_items:
                r["email_verified_at"] = None

        cur.execute("SELECT COUNT(*) AS c FROM external_login_user")
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
        where.append(
            "(nickname LIKE %s OR x_id LIKE %s OR instagram_id LIKE %s OR email LIKE %s OR social_id LIKE %s)"
        )
        params += [like, like, like, like, like]
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""

    has_verified = _column_exists("external_login_user", "email_verified_at")
    cols = """
        id, nickname, x_id, instagram_id, email, social_id,
        avatar_file, avatar_url, created_at, updated_at,
        admin_note
    """
    if has_verified:
        cols += ", email_verified_at"

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute(f"SELECT COUNT(*) AS c FROM external_login_user {sql_where}", params)
        total = int(cur.fetchone()["c"])
        offset = (page - 1) * per_page

        # ★ 一覧と同じ並び順に統一
        cur.execute(f"""
            SELECT {cols}
              FROM external_login_user
              {sql_where}
             ORDER BY
               CASE
                 WHEN nickname REGEXP '^[0-9A-Za-z]' THEN 0
                 WHEN nickname REGEXP '^[ぁ-ゟァ-ヿー]' THEN 1
                 ELSE 2
               END,
               nickname
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

# ============= API（単票：旧モーダル用） =============
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
              id,
              nickname,
              x_id,
              instagram_id,
              email,
              social_id,
              avatar_file,
              avatar_url,
              created_at,
              updated_at,
              admin_note
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
    email      = (data.get("email") or "").strip() or None
    admin_note = (data.get("admin_note") or "").strip() or None  # ★追加: 内部メモ

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
        cur.execute("SELECT email FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
        row = cur.fetchone()
        if not row:
            if request.is_json:
                return jsonify({"ok": False, "error": "not_found"}), 404
            return redirect(url_for("external_login_user.admin_ext_users_index"))

        email_before = (row[0] if isinstance(row, tuple) else row.get("email")) if row else None
        email_changed = (email or None) != (email_before or None)

        if email_changed and has_verified:
            cur.execute("""
                UPDATE external_login_user
                   SET nickname=%s,
                       x_id=%s,
                       instagram_id=%s,
                       email=%s,
                       admin_note=%s,
                       email_verified_at=NULL
                 WHERE id=%s
                 LIMIT 1
            """, (nickname, x_id_raw or None, ig_raw or None, email, admin_note, user_id))
        else:
            cur.execute("""
                UPDATE external_login_user
                   SET nickname=%s,
                       x_id=%s,
                       instagram_id=%s,
                       email=%s,
                       admin_note=%s
                 WHERE id=%s
                 LIMIT 1
            """, (nickname, x_id_raw or None, ig_raw or None, email, admin_note, user_id))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    # 成功時の応答
    if request.is_json:
        return jsonify({"ok": True})
    # フォームは一覧へ
    return redirect(url_for("external_login_user.admin_ext_users_index", saved=1))


# ============= 再送（送信） =============
@bp.post("/admin/ext-users/<int:user_id>/resend-verify")
def admin_ext_users_resend_verify(user_id: int):
    """
    確認メールの再送
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

    if not _issue_email_verify_token:
        if request.is_json:
            return jsonify({"ok": False, "error": "token_issuer_unavailable"}), 500
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="token"))

    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT email FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
        row = cur.fetchone()
        if not row:
            if request.is_json:
                return jsonify({"ok": False, "error": "not_found"}), 404
            return redirect(url_for("external_login_user.admin_ext_users_index"))

        email = row[0] if isinstance(row, tuple) else row.get("email")
        if not email:
            if request.is_json:
                return jsonify({"ok": False, "error": "no_email"}), 400
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="no_email"))

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

        # 送信（/app/utils/mail.py）
        try:
            send_mail(
                to=email,
                subject=subject,
                body=body,
                event_uuid=None,  # From: noreply@mail.iori0624.jp
                reply_to=None,
            )
        except Exception as e:
            if request.is_json:
                return jsonify({"ok": False, "error": "smtp_error", "detail": str(e)}), 502
            return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, error="smtp"))

        if request.is_json:
            return jsonify({"ok": True})
        return redirect(url_for("external_login_user.admin_ext_users_edit_page", user_id=user_id, sent="1"))

    finally:
        try:
            cur.close()
            db.close()
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
        cur.execute("SELECT id, email FROM external_login_user WHERE id=%s LIMIT 1", (user_id,))
        u = cur.fetchone()
        if not u:
            abort(404)
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
        cols = """
            id, nickname, x_id, instagram_id, email, social_id,
            avatar_file, avatar_url, created_at, updated_at,
            admin_note,
            COALESCE(notify_album_upload, 1)  AS notify_album_upload,
            COALESCE(notify_album_process, 1) AS notify_album_process
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
              m.payment_status
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

    finally:
        try:
            cur.close()
            db.close()
        except Exception:
            pass

    return render_template(
        "admin_ext_users_edit.html",
        u=user,
        memberships=memberships,
        assignable_events=assignable,
        admin_csrf=_admin_csrf_token()
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
        # すでに参加済みか軽く確認（UNIQUE制約もあるが事前チェックでメッセージを素直に）
        cur.execute("SELECT 1 FROM mfu_event_member WHERE event_id=%s AND user_id=%s", (event_id, user_id))
        if cur.fetchone():
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

# ============= 参加削除 確認ページ（GET） =============
@bp.get("/admin/ext-users/<int:user_id>/membership/<int:member_id>/delete-confirm",
        endpoint="admin_ext_users_member_delete_confirm")
def admin_ext_users_member_delete_confirm(user_id: int, member_id: int):
    """ユーザーのイベント参加レコードを削除する前の確認ページ"""
    guard = _require_mfu_login_redirect()
    if guard:
        return guard

    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        # 該当の membership がユーザーのものか検証して情報取得
        cur.execute("""
            SELECT m.id AS member_id, m.user_id, m.event_id, m.status, m.payment_status,
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
    """ユーザーのイベント参加レコードを削除する"""
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
        # 所有確認してから削除
        cur.execute("SELECT 1 FROM mfu_event_member WHERE id=%s AND user_id=%s LIMIT 1",
                    (member_id, user_id))
        if not cur.fetchone():
            return redirect(url_for("external_login_user.admin_ext_users_edit_page",
                                    user_id=user_id, error="not_found"))

        cur.execute("DELETE FROM mfu_event_member WHERE id=%s LIMIT 1", (member_id,))
        db.commit()
    finally:
        try: cur.close(); db.close()
        except Exception: pass

    return redirect(url_for("external_login_user.admin_ext_users_edit_page",
                            user_id=user_id, deleted="1"))

