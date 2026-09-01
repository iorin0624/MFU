from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from flask import Blueprint, abort, jsonify, redirect, render_template, render_template_string, request, session, url_for

from app.utils.db import get_db


uploader_auth_bp = Blueprint("uploader_auth", __name__, url_prefix="/desktop/uploader")
uploader_admin_bp = Blueprint("uploader_admin", __name__)

TOKEN_PREFIX = "mfu_up_"
TOKEN_DAYS = 180
TOKEN_SCOPE_DESKTOP = "desktop_upload"
TOKEN_SCOPE_IOS = "ios_shortcut_upload"
VALID_TOKEN_SCOPES = {TOKEN_SCOPE_DESKTOP, TOKEN_SCOPE_IOS}
_schema_ready = False
JST = timezone(timedelta(hours=9))


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _format_utc_as_jst(value: datetime | None) -> str:
    """Format a timezone-naive MySQL UTC DATETIME for the admin UI."""
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(JST).strftime("%Y年%m月%d日 %H:%M:%S")


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS uploader_tokens (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          token_hash CHAR(64) NOT NULL,
          username VARCHAR(191) NOT NULL,
          label VARCHAR(120) NOT NULL DEFAULT 'MFU Uploader',
          scope VARCHAR(32) NOT NULL DEFAULT 'desktop_upload',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          expires_at DATETIME NULL,
          last_used_at DATETIME NULL,
          revoked_at DATETIME NULL,
          PRIMARY KEY (id),
          UNIQUE KEY uq_uploader_tokens_hash (token_hash),
          KEY idx_uploader_tokens_username (username),
          KEY idx_uploader_tokens_expires (expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute("SHOW COLUMNS FROM uploader_tokens LIKE 'scope'")
    if not cur.fetchone():
        cur.execute(
            "ALTER TABLE uploader_tokens "
            "ADD COLUMN scope VARCHAR(32) NOT NULL DEFAULT 'desktop_upload' AFTER label"
        )
    db.commit()
    db.close()
    _schema_ready = True


def _valid_callback(value: str) -> str:
    parsed = urlparse(value or "")
    if parsed.scheme != "http":
        return ""
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return ""
    if not parsed.port:
        return ""
    return value


def _bearer_token() -> str:
    header = request.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return ""
    return header.split(" ", 1)[1].strip()


def issue_uploader_token(
    username: str,
    label: str = "MFU Uploader",
    *,
    scope: str = TOKEN_SCOPE_DESKTOP,
) -> str:
    _ensure_schema()
    if scope not in VALID_TOKEN_SCOPES:
        raise ValueError("invalid uploader token scope")
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    expires_at = datetime.utcnow() + timedelta(days=TOKEN_DAYS)
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO uploader_tokens
            (token_hash, username, label, scope, created_at, expires_at, last_used_at, revoked_at)
        VALUES (%s, %s, %s, %s, UTC_TIMESTAMP(), %s, NULL, NULL)
        """,
        (token_hash, username, label[:120], scope, expires_at),
    )
    db.commit()
    db.close()
    return token


def verify_uploader_token(
    token: str | None = None,
    *,
    allowed_scopes: set[str] | None = None,
) -> dict | None:
    raw = (token or _bearer_token()).strip()
    if not raw or not raw.startswith(TOKEN_PREFIX):
        return None
    _ensure_schema()
    token_hash = _hash_token(raw)
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, username, label, scope, expires_at, revoked_at
          FROM uploader_tokens
         WHERE token_hash = %s
         LIMIT 1
        """,
        (token_hash,),
    )
    row = cur.fetchone()
    if not row or row.get("revoked_at"):
        db.close()
        return None
    scope = str(row.get("scope") or TOKEN_SCOPE_DESKTOP)
    if allowed_scopes is not None and scope not in allowed_scopes:
        db.close()
        return None
    expires_at = row.get("expires_at")
    if expires_at and expires_at < datetime.utcnow():
        db.close()
        return None
    cur.execute("UPDATE uploader_tokens SET last_used_at = UTC_TIMESTAMP() WHERE id = %s", (row["id"],))
    db.commit()
    db.close()
    return row


def revoke_uploader_token(token: str) -> bool:
    if not token:
        return False
    _ensure_schema()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE uploader_tokens SET revoked_at = UTC_TIMESTAMP() WHERE token_hash = %s AND revoked_at IS NULL",
        (_hash_token(token),),
    )
    changed = cur.rowcount > 0
    db.commit()
    db.close()
    return changed


def list_uploader_tokens(username: str, *, scope: str | None = None) -> list[dict]:
    _ensure_schema()
    db = get_db()
    cur = db.cursor(dictionary=True)
    if scope:
        cur.execute(
            """
            SELECT id, username, label, scope, created_at, expires_at, last_used_at, revoked_at
              FROM uploader_tokens
             WHERE username = %s AND scope = %s
             ORDER BY id DESC
            """,
            (username, scope),
        )
    else:
        cur.execute(
            """
            SELECT id, username, label, scope, created_at, expires_at, last_used_at, revoked_at
              FROM uploader_tokens
             WHERE username = %s
             ORDER BY id DESC
            """,
            (username,),
        )
    rows = cur.fetchall() or []
    cur.close()
    db.close()
    return rows


def revoke_uploader_token_by_id(username: str, token_id: int, *, scope: str | None = None) -> bool:
    _ensure_schema()
    db = get_db()
    cur = db.cursor()
    params: list[object] = [username, int(token_id)]
    sql = (
        "UPDATE uploader_tokens SET revoked_at = UTC_TIMESTAMP() "
        "WHERE username = %s AND id = %s AND revoked_at IS NULL"
    )
    if scope:
        sql += " AND scope = %s"
        params.append(scope)
    cur.execute(sql, tuple(params))
    changed = cur.rowcount > 0
    db.commit()
    cur.close()
    db.close()
    return changed


@uploader_admin_bp.route("/admin/ios-shortcut-upload", methods=["GET", "POST"])
def ios_shortcut_upload_admin():
    if str(session.get("user") or "") != "admin":
        abort(403)

    created_key = ""
    notice = ""
    if request.method == "POST":
        action = str(request.form.get("action") or "").strip()
        if action == "create":
            label = str(request.form.get("label") or "").strip() or "iPhone Shortcut"
            created_key = issue_uploader_token(
                "admin",
                label=label,
                scope=TOKEN_SCOPE_IOS,
            )
            notice = "APIキーを発行しました。この画面を閉じると再表示できません。"
        elif action == "revoke":
            try:
                token_id = int(request.form.get("token_id") or 0)
            except (TypeError, ValueError):
                token_id = 0
            if token_id and revoke_uploader_token_by_id(
                "admin", token_id, scope=TOKEN_SCOPE_IOS
            ):
                notice = "APIキーを無効化しました。"
            else:
                notice = "対象のAPIキーは既に無効か、見つかりません。"

    tokens = list_uploader_tokens("admin", scope=TOKEN_SCOPE_IOS)
    for token in tokens:
        token["created_at_jst"] = _format_utc_as_jst(token.get("created_at"))
        token["last_used_at_jst"] = _format_utc_as_jst(token.get("last_used_at"))
        token["expires_at_jst"] = _format_utc_as_jst(token.get("expires_at"))
    return render_template(
        "admin_ios_shortcut_upload.html",
        tokens=tokens,
        created_key=created_key,
        notice=notice,
    )


@uploader_auth_bp.get("/login/start")
def login_start():
    callback = _valid_callback(request.args.get("callback") or "")
    state = (request.args.get("state") or "").strip()
    if not callback or not state:
        return "Invalid callback", 400
    if not session.get("user"):
        next_url = url_for("uploader_auth.login_start", callback=callback, state=state)
        return redirect(url_for("login", next=next_url))
    return render_template_string(
        """
        <!doctype html>
        <html lang="ja">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>MFU Uploader 認可</title>
          <style>
            body{font-family:system-ui,"Yu Gothic",Meiryo,sans-serif;background:#f6f7fb;color:#1f2937;margin:0;min-height:100vh;display:grid;place-items:center}
            main{width:min(560px,calc(100vw - 32px));background:white;border:1px solid #d1d5db;border-radius:10px;box-shadow:0 18px 45px rgba(15,23,42,.14);padding:28px}
            h1{font-size:22px;margin:0 0 14px}
            p{line-height:1.7}
            .user{font-weight:700}
            .actions{display:flex;gap:10px;justify-content:flex-end;margin-top:24px}
            button,a{font:inherit}
            button{background:#0b63dd;color:white;border:0;border-radius:7px;padding:10px 16px;cursor:pointer}
            a{color:#4b5563;text-decoration:none;padding:10px 12px}
          </style>
        </head>
        <body>
          <main>
            <h1>MFU Uploader を許可しますか？</h1>
            <p><span class="user">{{ username }}</span> として、このWindowsアプリにアップロード枠作成、原本アップロード、サムネイルアップロード、完了通知の実行を許可します。</p>
            <p>許可すると、このアプリ専用のAPIトークンが発行されます。ChromeのCookieやパスワードはアプリに保存されません。</p>
            <form method="post" action="{{ url_for('uploader_auth.login_approve') }}" class="actions">
              <input type="hidden" name="callback" value="{{ callback }}">
              <input type="hidden" name="state" value="{{ state }}">
              <a href="{{ url_for('index') }}">キャンセル</a>
              <button type="submit">許可する</button>
            </form>
          </main>
        </body>
        </html>
        """,
        username=session.get("user"),
        callback=callback,
        state=state,
    )


@uploader_auth_bp.post("/login/approve")
def login_approve():
    callback = _valid_callback(request.form.get("callback") or "")
    state = (request.form.get("state") or "").strip()
    username = session.get("user")
    if not callback or not state:
        return "Invalid callback", 400
    if not username:
        next_url = url_for("uploader_auth.login_start", callback=callback, state=state)
        return redirect(url_for("login", next=next_url))
    token = issue_uploader_token(username)
    sep = "&" if "?" in callback else "?"
    return redirect(callback + sep + urlencode({"token": token, "state": state}))


@uploader_auth_bp.get("/api/session")
def api_session():
    row = verify_uploader_token()
    if not row:
        return jsonify({"ok": False, "authenticated": False, "error": "invalid_token"}), 401
    return jsonify({"ok": True, "authenticated": True, "username": row.get("username"), "label": row.get("label")})


@uploader_auth_bp.post("/api/revoke")
def api_revoke():
    token = _bearer_token()
    if not token:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    revoke_uploader_token(token)
    return jsonify({"ok": True})
