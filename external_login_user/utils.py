# -*- coding: utf-8 -*-
from __future__ import annotations
import os, re, uuid, base64, secrets, hashlib
from datetime import datetime, timedelta
from typing import Optional, Any
from urllib.parse import quote_plus
from flask import current_app, request, session, redirect, url_for, abort, flash

from . import bp, oauth  # oauth は None の可能性あり
from app.utils.db import get_db

QR_TRADEMARK_NOTICE = "QRコードは株式会社デンソーウェーブの登録商標です。"
SESSION_MAP_MAX_ITEMS = 8
SESSION_VALUE_MAX_LEN = 128
EXT_LOGIN_MODE_BROWSER = "browser"
EXT_LOGIN_MODE_PWA = "pwa"
PWA_RESUME_TOKEN_TTL_SECONDS = 300
PWA_RESUME_LOCAL_STORAGE_TOKEN_KEY = "mfu_pwa_resume_token"
PWA_RESUME_LOCAL_STORAGE_ISSUED_AT_KEY = "mfu_pwa_resume_at"
PWA_RESUME_LOCAL_STORAGE_CLIENT_ID_KEY = "mfu_pwa_client_id"

# ---- 環境値 → 関数 ----
def LINE_CLIENT_ID() -> str:
    return os.getenv("LINE_CHANNEL_ID") or ""

def LINE_CLIENT_SECRET() -> str:
    return os.getenv("LINE_CHANNEL_SECRET") or ""

def LINE_REDIRECT_URI() -> Optional[str]:
    v = os.getenv("LINE_REDIRECT_URI")
    return v.strip() if v else None

def PAYMENT_ENTRY_BASE() -> str:
    return (os.getenv("PAYMENT_ENTRY_BASE") or "/payment/e/").rstrip("/") + "/"

# ---- CSRF, セッション ----
def _admin_csrf_token() -> str:
    t = session.get("admin_csrf")
    if not t:
        t = secrets.token_urlsafe(16)
        session["admin_csrf"] = t
    return t

def _require_mfu_login_redirect():
    if not session.get("user"):
        flash("管理者ページはMFUログインが必要です。", "warning")
        return redirect("/login")
    return None

def _to_local_next(u: str) -> str:
    # ここに“ログイン後に戻したい”プレフィックスを追加
    ALLOW_PREFIXES = (
        "/external-login/",      # 既定
        # "/albums/share/",      # 例：共有アルバム系も戻したい場合
        # "/payments/",          # 例：決済フローも戻したい場合
    )

    if not u:
        return "/external-login/"

    from urllib.parse import urlparse
    try:
        p = urlparse(u)
        path = p.path or ""
        qs   = ("?" + p.query) if p.query else ""
    except Exception:
        # 解析できない文字列は破棄
        return "/external-login/"

    # ローカル絶対パスのみ許可（//で始まるスキーム相対URLは拒否）
    if path.startswith("/") and not path.startswith("//"):
        if any(path.startswith(pre) for pre in ALLOW_PREFIXES):
            return path + qs

    # 不適切なものは既定にフォールバック
    return "/external-login/"


def _require_ext_login():
    """未ログインなら安全化した next を保持して LINE ログインへ"""
    if session.get("ext_user_id"):
        return None

    raw_next = request.url  # 例: /external-login/events/view/<uuid>?iv=...

    # 絶対URLや外部ドメインを排除し、/external-login/ 配下の相対URLだけ許可
    from urllib.parse import urlparse
    def _to_local_next(u: str) -> str:
        if not u:
            return "/external-login/"
        if u.startswith("/") and not u.startswith("//"):
            return u
        try:
            p = urlparse(u)
            if (p.path or "").startswith("/") and "/external-login/" in (p.path or ""):
                return p.path + (("?" + p.query) if p.query else "")
        except Exception:
            pass
        return "/external-login/"

    local_next = _to_local_next(raw_next)[:512]
    session["ext_after_login_next"] = local_next
    return redirect(url_for("external_login_user.line_login", next=local_next))

def _is_mfu_logged_in() -> bool:
    return bool(session.get("user"))


def remember_session_map_value(map_key: str, item_key: str, value: Any, *, max_items: int = SESSION_MAP_MAX_ITEMS):
    """cookie session の肥大化防止のため、少数件だけ保持する。"""
    key = str(item_key or "").strip()
    if not key:
        return

    stored = session.get(map_key)
    data = dict(stored) if isinstance(stored, dict) else {}
    data.pop(key, None)

    item = value
    if isinstance(item, str):
        item = item[:SESSION_VALUE_MAX_LEN]
    data[key] = item

    while len(data) > max_items:
        oldest = next(iter(data), None)
        if oldest is None:
            break
        data.pop(oldest, None)
    session[map_key] = data


def set_compact_pay_ctx(*, event_id: int, event_uuid: str | None, ext_user_id: int, expected_amount_yen: int | None, payment_token: str, invite_token: str | None = None):
    """セッションには識別に必要な最小情報のみを保存する。"""
    ctx = {
        "mfu_event_id": int(event_id),
        "mfu_event_uuid": (event_uuid or "")[:64],
        "ext_user_id": int(ext_user_id),
        "expected_amount_yen": int(expected_amount_yen) if expected_amount_yen is not None else None,
        "payment_token": (payment_token or "")[:128],
    }
    if invite_token:
        ctx["invite_token"] = invite_token[:SESSION_VALUE_MAX_LEN]
    session["pay_ctx"] = ctx


def normalize_ext_login_mode(raw_mode: Any) -> str:
    value = str(raw_mode or "").strip().lower()
    return EXT_LOGIN_MODE_PWA if value in {"1", "true", "yes", "on", EXT_LOGIN_MODE_PWA} else EXT_LOGIN_MODE_BROWSER


def is_pwa_login_request() -> bool:
    return normalize_ext_login_mode(request.args.get("pwa") or session.get("ext_login_mode")) == EXT_LOGIN_MODE_PWA


def issue_pwa_client_id() -> str:
    return secrets.token_urlsafe(24)


def _resume_token_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _utcnow_naive() -> datetime:
    return datetime.utcnow().replace(tzinfo=None)


def create_external_login_resume_token(
    *,
    ext_user_id: int,
    social_id: str,
    next_path: str,
    mode: str,
    pwa_client_id: str | None = None,
    ttl_seconds: int = PWA_RESUME_TOKEN_TTL_SECONDS,
) -> str:
    token = secrets.token_urlsafe(32)
    now = _utcnow_naive()
    expires_at = now + timedelta(seconds=max(30, int(ttl_seconds or PWA_RESUME_TOKEN_TTL_SECONDS)))
    token_hash = _resume_token_hash(token)
    client_hash = _resume_token_hash(pwa_client_id) if pwa_client_id else None

    db = get_db(); cur = db.cursor()
    try:
        cur.execute(
            """
            INSERT INTO external_login_resume_token
              (token_hash, ext_user_id, social_id, next_path, mode, pwa_client_id_hash,
               issued_at, expires_at, consumed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)
            """,
            (
                token_hash,
                int(ext_user_id),
                (social_id or "")[:191],
                (next_path or "/external-login/")[:512],
                normalize_ext_login_mode(mode),
                client_hash,
                now,
                expires_at,
            ),
        )
        db.commit()
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass
    return token


def _fetch_resume_token_row(
    *,
    token: str | None = None,
    pwa_client_id: str | None = None,
    for_update: bool = False,
) -> dict[str, Any] | None:
    clauses: list[str] = [
        "consumed_at IS NULL",
        "expires_at >= %s",
    ]
    params: list[Any] = [_utcnow_naive()]

    token_hash = _resume_token_hash(token) if token else None
    client_hash = _resume_token_hash(pwa_client_id) if pwa_client_id else None
    if token_hash:
        clauses.append("token_hash = %s")
        params.append(token_hash)
    elif client_hash:
        clauses.append("pwa_client_id_hash = %s")
        params.append(client_hash)
        clauses.append("mode = %s")
        params.append(EXT_LOGIN_MODE_PWA)
    else:
        return None

    order_by = "ORDER BY issued_at DESC, id DESC"
    limit = "LIMIT 1"
    lock = " FOR UPDATE" if for_update else ""
    sql = f"""
        SELECT
          id, ext_user_id, social_id, next_path, mode,
          issued_at, expires_at, consumed_at, created_at, updated_at
        FROM external_login_resume_token
        WHERE {' AND '.join(clauses)}
        {order_by}
        {limit}{lock}
    """
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute(sql, tuple(params))
        row = cur.fetchone()
        if for_update:
            return {"_db": db, "_cur": cur, "row": row}
        return row
    except Exception:
        try:
            cur.close(); db.close()
        except Exception:
            pass
        raise


def get_external_login_resume_token_summary(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    return _fetch_resume_token_row(token=token, for_update=False)


def consume_external_login_resume_token(*, token: str | None = None, pwa_client_id: str | None = None) -> dict[str, Any] | None:
    locked = _fetch_resume_token_row(token=token, pwa_client_id=pwa_client_id, for_update=True)
    if not locked:
        return None

    db = locked["_db"]
    cur = locked["_cur"]
    row = locked["row"]
    if not row:
        try:
            db.rollback()
        except Exception:
            pass
        try:
            cur.close(); db.close()
        except Exception:
            pass
        return None

    consumed_at = _utcnow_naive()
    try:
        cur.execute(
            """
            UPDATE external_login_resume_token
               SET consumed_at=%s,
                   updated_at=CURRENT_TIMESTAMP
             WHERE id=%s
               AND consumed_at IS NULL
             LIMIT 1
            """,
            (consumed_at, int(row["id"])),
        )
        if int(cur.rowcount or 0) != 1:
            db.rollback()
            return None
        db.commit()
        row["consumed_at"] = consumed_at
        return row
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

# ---- ID/DB ヘルパ ----
def _uuid_bytes_to_str(b: bytes | None) -> Optional[str]:
    if not b: return None
    try:
        return str(uuid.UUID(bytes=b))
    except Exception:
        try:
            return str(uuid.UUID(hex=b.hex()))
        except Exception:
            return None

def _get_ext_user_by_social(social_id: str) -> Optional[dict]:
    """
    external_login_user を social_id で1件取得して dict で返す。
    avatar_url / avatar_file も取得して、イベント参加証などでのアイコン表示に使えるようにする。
    """
    db = get_db(); cur = db.cursor()
    cur.execute("""
        SELECT
          id, mfu_uuid, social_id, nickname, x_id, instagram_id, email,
          avatar_url, avatar_file,
          created_at, updated_at
        FROM external_login_user
        WHERE social_id=%s
        LIMIT 1
    """, (social_id,))
    row = cur.fetchone()

    if not row:
        cur.close(); db.close()
        return None

    # カラム名をカーソルから動的取得（dictカーソルでなくてもOK）
    try:
        col_names = [d[0] for d in (cur.description or [])]
        d = dict(zip(col_names, row))
    except Exception:
        # タプルでも安全に復元
        keys = ["id","mfu_uuid","social_id","nickname","x_id","instagram_id","email",
                "avatar_url","avatar_file","created_at","updated_at"]
        d = dict(zip(keys, row))

    cur.close(); db.close()

    # 互換フィールド
    d["mfu_uuid_str"] = _uuid_bytes_to_str(d.get("mfu_uuid"))
    return d

def _upsert_ext_user(*, social_id: str, nickname: str,
                     email: Optional[str], x_id: Optional[str], instagram_id: Optional[str]) -> None:
    sql = """
    INSERT INTO external_login_user
      (mfu_uuid, social_id, nickname, x_id, instagram_id, email)
    VALUES (UNHEX(REPLACE(UUID(),'-','')), %s, %s, %s, %s, %s)
    ON DUPLICATE KEY UPDATE
      nickname=VALUES(nickname), x_id=VALUES(x_id), instagram_id=VALUES(instagram_id),
      email=VALUES(email), updated_at=CURRENT_TIMESTAMP
    """
    db = get_db(); cur = db.cursor()
    cur.execute(sql, (social_id, nickname, x_id, instagram_id, email))
    db.commit(); cur.close(); db.close()

def _update_profile(user_id: int, nickname: str,
                    x_id: Optional[str], instagram_id: Optional[str], email: Optional[str]) -> None:
    db = get_db(); cur = db.cursor()
    cur.execute("UPDATE external_login_user SET nickname=%s, x_id=%s, instagram_id=%s, email=%s WHERE id=%s",
                (nickname, x_id, instagram_id, email, user_id))
    db.commit(); cur.close(); db.close()

def _membership_status(event_id: int, ext_user_id: int) -> Optional[str]:
    db = get_db(); cur = db.cursor()
    cur.execute("SELECT status FROM mfu_event_member WHERE event_id=%s AND user_id=%s", (event_id, ext_user_id))
    row = cur.fetchone(); cur.close(); db.close()
    if not row: return None
    return row[0] if isinstance(row, tuple) else row["status"]

def _member_payment_status(event_id: int, ext_user_id: int) -> str:
    db = get_db(); cur = db.cursor()
    cur.execute("SELECT payment_status FROM mfu_event_member WHERE event_id=%s AND user_id=%s", (event_id, ext_user_id))
    row = cur.fetchone(); cur.close(); db.close()
    if not row: return "unpaid"
    return row[0] if isinstance(row, tuple) else row["payment_status"]


def update_event_member_status(
    event_id: int,
    user_id: int,
    new_status: str,
    *,
    extra_update_fields: dict[str, Any] | None = None,
    extra_insert_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    mfu_event_member.status の更新を共通化し、
    未承認→承認済み遷移時だけチャットへSystem投稿する。
    """
    if new_status not in {"approved", "pending", "rejected"}:
        raise ValueError("invalid status")

    extra_update_fields = dict(extra_update_fields or {})
    extra_insert_fields = dict(extra_insert_fields or {})

    db = get_db()
    cur = db.cursor(dictionary=True)
    before_status: str | None = None
    created = False
    try:
        cur.execute(
            "SELECT id, status FROM mfu_event_member WHERE event_id=%s AND user_id=%s LIMIT 1",
            (event_id, user_id),
        )
        row = cur.fetchone()
        if row:
            before_status = (row.get("status") or "").strip().lower() or None
            set_parts = ["status=%s"]
            values: list[Any] = [new_status]
            for field_name, field_value in extra_update_fields.items():
                set_parts.append(f"{field_name}=%s")
                values.append(field_value)
            values.extend([event_id, user_id])
            cur.execute(
                f"UPDATE mfu_event_member SET {', '.join(set_parts)} WHERE event_id=%s AND user_id=%s LIMIT 1",
                tuple(values),
            )
        else:
            created = True
            insert_fields = {"event_id": event_id, "user_id": user_id, "status": new_status}
            for key, value in extra_insert_fields.items():
                insert_fields[key] = value
            cols = list(insert_fields.keys())
            vals = [insert_fields[col] for col in cols]
            placeholders = ", ".join(["%s"] * len(cols))
            cur.execute(
                f"INSERT INTO mfu_event_member ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(vals),
            )
        db.commit()
    finally:
        cur.close()
        db.close()

    approved_transition = before_status != "approved" and new_status == "approved"
    suppress_join_approved_message = False
    if approved_transition:
        db = get_db()
        cur = db.cursor(dictionary=True)
        try:
            cur.execute("SELECT line_openchat_url FROM mfu_event WHERE id=%s LIMIT 1", (event_id,))
            ev = cur.fetchone() or {}
            suppress_join_approved_message = bool((ev.get("line_openchat_url") or "").strip())
        except Exception:
            current_app.logger.exception("line_openchat_url lookup failed event_id=%s", event_id)
        finally:
            cur.close()
            db.close()

    if approved_transition and not suppress_join_approved_message:
        try:
            from app.chat import get_external_user_display_name, post_system_message_to_event_main_room

            nickname = get_external_user_display_name(user_id)
            post_system_message_to_event_main_room(event_id, template_key="join_approved", nickname=nickname)
        except Exception:
            current_app.logger.exception(
                "join-approved system message failed event_id=%s user_id=%s",
                event_id,
                user_id,
            )

    return {
        "before_status": before_status,
        "after_status": new_status,
        "changed": before_status != new_status,
        "created": created,
        "approved_transition": approved_transition,
        "join_message_suppressed": suppress_join_approved_message,
    }

def _event_by_uuid_str(u: str) -> Optional[dict]:
    """
    mfu_event を UUID(文字列) で1件取得。
    ★ 重要変更点: SELECT * + cur.description を使って「自動で」dict化。
      → 今後カラムを追加しても、この関数は修正不要。
    さらに従来互換のため event_uuid_str を付加して返す。
    """
    db = get_db(); cur = db.cursor()
    cur.execute("SELECT * FROM mfu_event WHERE event_uuid = UNHEX(REPLACE(%s,'-',''))", (u,))
    row = cur.fetchone()
    if not row:
        cur.close(); db.close()
        return None

    # カラム名をカーソルから動的取得
    col_names = [desc[0] for desc in cur.description] if getattr(cur, "description", None) else []
    d = dict(zip(col_names, row)) if col_names else (dict(row) if not isinstance(row, tuple) else {})
    cur.close(); db.close()

    # 互換フィールド
    if d:
        d["event_uuid_str"] = _uuid_bytes_to_str(d.get("event_uuid"))
    return d or None

def _get_member_require_payment(event_id: int, user_id: int) -> int:
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("""
            SELECT COALESCE(require_payment, 1)
              FROM mfu_event_member
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (event_id, user_id))
        row = cur.fetchone()
        if not row:
            return 1
        v = row[0] if isinstance(row, tuple) else list(row.values())[0]  # どちらのカーソルでも耐性
        try:
            return int(v or 1)
        except Exception:
            return 1
    finally:
        cur.close(); db.close()

# utils.py に追記（どこでも呼べる共通関数）
from urllib.parse import urlencode

def avatar_url_for(user: dict | None) -> str | None:
    """
    external_login_user の1行（dict想定）から、キャッシュバスター付きの表示URLを返す。
    優先度: avatar_file (/avatars/..) -> avatar_url(外部URL)
    バージョンは updated_at（存在すれば）を使う。
    """
    if not user:
        return None

    # v= には updated_at を使う（なければ None）
    v = None
    try:
        ua = user.get("updated_at")
        if ua:
            # MySQL DATETIME でも str でもOKに揃える
            from datetime import datetime
            if hasattr(ua, "timestamp"):
                v = int(ua.timestamp())
            else:
                # "YYYY-MM-DD HH:MM:SS" など → epoch
                v = int(datetime.fromisoformat(str(ua).replace(" ", "T")).timestamp())
    except Exception:
        v = None

    if user.get("avatar_file"):
        qs = f"?{urlencode({'v': v})}" if v else ""
        return f"/external-login/avatars/{user['avatar_file']}{qs}"

    if user.get("avatar_url"):
        if v:
            sep = "&" if "?" in user["avatar_url"] else "?"
            return f"{user['avatar_url']}{sep}v={v}"
        return user["avatar_url"]

    return None

# ==== Discordワンクリック承認/拒否 用トークン ====
import hmac, time, hashlib, base64
from flask import current_app

def _sign_discord_action(event_id: int, user_id: int, action: str, *, ttl_sec: int = 24*3600) -> str:
    """
    event_id / user_id / action(approve|reject) / exp を束ねてHMAC-SHA256で署名。
    返り値は URL-safe base64 の "<payload>.<sig>"
    """
    exp = int(time.time()) + int(ttl_sec)
    payload = f"{event_id}.{user_id}.{action}.{exp}"
    secret = (current_app.config.get("DISCORD_ACTION_SECRET") or current_app.config.get("SECRET_KEY") or "mfu-default").encode("utf-8")
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=") + "." + base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")

def _verify_discord_action(token: str) -> tuple[int,int,str] | None:
    """
    token を検証して (event_id, user_id, action) を返す。期限切れ/改ざんは None。
    """
    try:
        b64_payload, b64_sig = token.split(".", 1)
        # パディング復元
        def _pad(s): return s + "=" * ((4 - len(s) % 4) % 4)
        payload = base64.urlsafe_b64decode(_pad(b64_payload)).decode("utf-8")
        exp = int(payload.rsplit(".", 1)[-1])
        if time.time() > exp:  # 期限
            return None
        secret = (current_app.config.get("DISCORD_ACTION_SECRET") or current_app.config.get("SECRET_KEY") or "mfu-default").encode("utf-8")
        sig_ok = hmac.compare_digest(
            base64.urlsafe_b64encode(hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).digest()).decode("ascii").rstrip("="),
            b64_sig
        )
        if not sig_ok:
            return None
        eid_s, uid_s, act, _ = payload.split(".")
        if act not in ("approve","reject"):
            return None
        return (int(eid_s), int(uid_s), act)
    except Exception:
        return None

# 公開（テンプレから呼ぶ）
@bp.app_template_global()
def my_role_label(role: str) -> str:
    m = {"none":"—","camera":"カメラマン","assistant":"アシスタント","cosplayer":"衣装"}
    return m.get((role or "none").lower(), "—")

# utils.py など（既存の import の近くでOK）
import re
from markupsafe import Markup, escape

@bp.app_template_filter("linkify")
def jinja_linkify(text: str | None):
    """
    プレーンテキスト内の http(s)://... を <a href=...> に変換。
    先に escape するので XSS 安全。戻りは Markup を返す。
    """
    if not text:
        return ""
    s = escape(text)  # まず全部エスケープ

    # URL検出（空白/<>\"') などで区切る。最後の句読点などは含めないように調整。
    pattern = re.compile(r'(https?://[^\s<>"\')\]]+)')

    def repl(m: re.Match):
        url = m.group(1)
        return Markup(f'<a href="{url}" target="_blank" rel="noopener nofollow">{url}</a>')

    return Markup(pattern.sub(repl, s))


# utils.py の import 群の下あたりに追加
def _mfu_username() -> str | None:
    from flask import session
    return (session.get("user") or "").strip() or None

def _event_acl_role(event_id: int, username: str | None) -> str | None:
    if not username:
        return None
    from app.utils.db import get_db
    db = get_db(); cur = db.cursor()
    try:
        cur.execute("SELECT role FROM mfu_event_admin_acl WHERE event_id=%s AND username=%s LIMIT 1",
                    (event_id, username))
        row = cur.fetchone()
        if not row:
            return None
        return row[0] if isinstance(row, tuple) else row.get("role")
    finally:
        try: cur.close(); db.close()
        except Exception: pass

def _event_admin_can_view(event_id: int) -> bool:
    """
    管理画面(外部ログインではなくMFUログイン側)での閲覧可否。
    admin はスーパーユーザーとして常に True。
    それ以外は ACL に存在すれば True。
    """
    u = _mfu_username()
    if u == "admin":   # グローバル管理者は全イベント参照可
        return True
    role = _event_acl_role(event_id, u)
    return bool(role)  # viewer / manager いずれも閲覧可

def _event_admin_can_manage(event_id: int) -> bool:
    """
    管理操作(編集・メンバー操作など)の可否。
    admin は True。ACL が manager のとき True。
    """
    u = _mfu_username()
    if u == "admin":
        return True
    return _event_acl_role(event_id, u) == "manager"

# 末尾あたりの便利関数群の近くに追記
def _ensure_event_invite_token(event_id: int) -> str:
    """イベントの invite_token が無い/空なら 256bit を発行して返す。"""
    from app.utils.db import get_db
    import os
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT invite_token FROM mfu_event WHERE id=%s", (event_id,))
        row = cur.fetchone()
        tok = (row and (row.get("invite_token") or "")) or ""
        if tok and len(tok) == 64 and tok == tok.lower():
            return tok  # 既存有効
        # 発行（256bit → 64桁hex小文字）
        tok = os.urandom(32).hex()
        cur.execute("UPDATE mfu_event SET invite_token=%s WHERE id=%s", (tok, event_id))
        db.commit()
        return tok
    finally:
        try: cur.close(); db.close()
        except Exception: pass


def _event_invite_url(ev: dict) -> str:
    """招待URL（join + iv=token）。ev は mfu_event の dict 想定。"""
    from flask import url_for
    base = url_for("external_login_user.join_event", event_uuid=ev["event_uuid_str"], _external=True)
    tok  = (ev.get("invite_token") or "").lower()
    return f"{base}?iv={tok}" if tok else base

# --- GoogleマップURL から緯度経度を抜き出すユーティリティ --------------------

def extract_lat_lng_from_maps_url(maps_url: str):
    """
    GoogleマップのURLから (lat, lng) を抜き出す。
    取れなければ (None, None) を返す。
    対応パターン:
      - https://www.google.com/maps/.../@35.681236,139.767125,17z/...
      - https://www.google.com/maps/search/?api=1&query=35.681236,139.767125
      - ...!3d35.681236!4d139.767125... （共有リンクの一部パターン）
    """
    if not maps_url:
        return (None, None)

    s = str(maps_url)

    # @35.681236,139.767125,17z
    m = re.search(r"@(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)", s)
    if m:
        try:
            lat = float(m.group(1))
            lng = float(m.group(2))
            return (lat, lng)
        except Exception:
            pass

    # ?q=35.681236,139.767125 または &q=35.681236,139.767125
    m = re.search(r"[?&]q=(-?\d{1,3}\.\d+),\s*(-?\d{1,3}\.\d+)", s)
    if m:
        try:
            lat = float(m.group(1))
            lng = float(m.group(2))
            return (lat, lng)
        except Exception:
            pass

    # ...!3d35.681236!4d139.767125...
    m = re.search(r"!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)", s)
    if m:
        try:
            lat = float(m.group(1))
            lng = float(m.group(2))
            return (lat, lng)
        except Exception:
            pass

    # どれにもマッチしなければ諦める
    return (None, None)
