from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse

from flask import Blueprint, jsonify, redirect, render_template_string, request, session, url_for

from app.utils.db import get_db


media_clipboard_bp = Blueprint("media_clipboard_auth", __name__, url_prefix="/desktop/media-clipboard")

TOKEN_PREFIX = "mfu_mc_"
TOKEN_DAYS = 180
_schema_ready = False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS media_clipboard_tokens (
          id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
          token_hash CHAR(64) NOT NULL,
          username VARCHAR(191) NOT NULL,
          label VARCHAR(120) NOT NULL DEFAULT 'MFU Media Clipboard',
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          expires_at DATETIME NULL,
          last_used_at DATETIME NULL,
          revoked_at DATETIME NULL,
          PRIMARY KEY (id),
          UNIQUE KEY uq_media_clipboard_tokens_hash (token_hash),
          KEY idx_media_clipboard_tokens_username (username),
          KEY idx_media_clipboard_tokens_expires (expires_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
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


def issue_media_clipboard_token(username: str, label: str = "MFU Media Clipboard") -> str:
    _ensure_schema()
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    expires_at = datetime.utcnow() + timedelta(days=TOKEN_DAYS)
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO media_clipboard_tokens
            (token_hash, username, label, created_at, expires_at, last_used_at, revoked_at)
        VALUES (%s, %s, %s, UTC_TIMESTAMP(), %s, NULL, NULL)
        """,
        (token_hash, username, label[:120], expires_at),
    )
    db.commit()
    db.close()
    return token


def verify_media_clipboard_token(token: str | None = None) -> dict | None:
    raw = (token or _bearer_token()).strip()
    if not raw or not raw.startswith(TOKEN_PREFIX):
        return None
    _ensure_schema()
    token_hash = _hash_token(raw)
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute(
        """
        SELECT id, username, label, expires_at, revoked_at
          FROM media_clipboard_tokens
         WHERE token_hash = %s
         LIMIT 1
        """,
        (token_hash,),
    )
    row = cur.fetchone()
    if not row or row.get("revoked_at"):
        db.close()
        return None
    expires_at = row.get("expires_at")
    if expires_at and expires_at < datetime.utcnow():
        db.close()
        return None
    cur.execute("UPDATE media_clipboard_tokens SET last_used_at = UTC_TIMESTAMP() WHERE id = %s", (row["id"],))
    db.commit()
    db.close()
    return row


def revoke_media_clipboard_token(token: str) -> bool:
    if not token:
        return False
    _ensure_schema()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE media_clipboard_tokens SET revoked_at = UTC_TIMESTAMP() WHERE token_hash = %s AND revoked_at IS NULL",
        (_hash_token(token),),
    )
    changed = cur.rowcount > 0
    db.commit()
    db.close()
    return changed


@media_clipboard_bp.get("/login/start")
def login_start():
    callback = _valid_callback(request.args.get("callback") or "")
    state = (request.args.get("state") or "").strip()
    if not callback or not state:
        return "Invalid callback", 400
    if not session.get("user"):
        next_url = url_for("media_clipboard_auth.login_start", callback=callback, state=state)
        return redirect(url_for("login", next=next_url))
    return render_template_string(
        """
        <!doctype html>
        <html lang="ja">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>MFU Media Clipboard 認可</title>
          <style>
            body{font-family:system-ui,"Yu Gothic",Meiryo,sans-serif;background:#f6f7fb;color:#1f2937;margin:0;min-height:100vh;display:grid;place-items:center}
            main{width:min(520px,calc(100vw - 32px));background:white;border:1px solid #d1d5db;border-radius:10px;box-shadow:0 18px 45px rgba(15,23,42,.14);padding:28px}
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
            <h1>MFU Media Clipboard を許可しますか？</h1>
            <p><span class="user">{{ username }}</span> として、このWindows常駐アプリに Image Viewer の画像・動画取得と保存を許可します。</p>
            <p>許可すると、このアプリ専用のAPIトークンが発行されます。ChromeのCookieはアプリに渡されません。</p>
            <form method="post" action="{{ url_for('media_clipboard_auth.login_approve') }}" class="actions">
              <input type="hidden" name="callback" value="{{ callback }}">
              <input type="hidden" name="state" value="{{ state }}">
              <a href="{{ url_for('image_viewer.index') }}">キャンセル</a>
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


@media_clipboard_bp.post("/login/approve")
def login_approve():
    callback = _valid_callback(request.form.get("callback") or "")
    state = (request.form.get("state") or "").strip()
    username = session.get("user")
    if not callback or not state:
        return "Invalid callback", 400
    if not username:
        next_url = url_for("media_clipboard_auth.login_start", callback=callback, state=state)
        return redirect(url_for("login", next=next_url))
    token = issue_media_clipboard_token(username)
    sep = "&" if "?" in callback else "?"
    return redirect(callback + sep + urlencode({"token": token, "state": state}))


@media_clipboard_bp.get("/api/session")
def api_session():
    row = verify_media_clipboard_token()
    if not row:
        return jsonify({"ok": False, "authenticated": False, "error": "invalid_token"}), 401
    return jsonify({"ok": True, "authenticated": True, "username": row.get("username"), "label": row.get("label")})


@media_clipboard_bp.post("/api/revoke")
def api_revoke():
    token = _bearer_token()
    if not token:
        return jsonify({"ok": False, "error": "invalid_token"}), 401
    revoke_media_clipboard_token(token)
    return jsonify({"ok": True})
