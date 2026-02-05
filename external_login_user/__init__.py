# -*- coding: utf-8 -*-
from __future__ import annotations
import os
import logging
from pathlib import Path
from flask import Blueprint
from flask import request, flash, redirect, url_for, current_app, g

from .ext_session import get_ext_session, save_ext_session


# --- env ロード（ローカル .env を優先しない） ---
def _load_local_env():
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except Exception:
        # 依存なし簡易ローダ
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        except Exception:
            pass

_load_local_env()

# --- Blueprint（テンプレートは従来どおり template/） ---
bp = Blueprint("external_login_user", __name__, template_folder="template")


@bp.before_request
def _load_external_session():
    get_ext_session()
    return None


@bp.after_request
def _store_external_session(response):
    ext_session = getattr(g, "ext_session", None)
    if ext_session is not None:
        return save_ext_session(response, ext_session)
    return response

# --- OAuth（Authlib は任意） ---
try:
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth()
except Exception:
    OAuth = None  # type: ignore
    oauth = None  # type: ignore

# 公開: app.__init__ から従来の init_oauth を呼べるように維持
def init_oauth(app=None, *, silent=True):
    """
    Backward-compatible initializer imported by app.__init__.

    - Loads local .env (done above)
    - Copies LINE_* envs into app.config if 'app' is provided
    - Returns True so the caller won't crash even if values are missing
    - If silent=False, raises RuntimeError when required keys are missing
    """
    keys = ("LINE_CHANNEL_ID", "LINE_CHANNEL_SECRET", "LINE_REDIRECT_URI")
    if app is not None:
        for k in keys:
            v = os.environ.get(k)
            if v:
                app.config[k] = v

    missing = [k for k in ("LINE_CHANNEL_ID", "LINE_CHANNEL_SECRET")
               if not (os.environ.get(k) or (app and app.config.get(k)))]
    if missing:
        msg = "LINE OAuth env missing: " + ", ".join(missing)
        logging.warning(msg)
        if not silent:
            raise RuntimeError(msg)

    # Authlib を使う場合のみ register
    if app is not None and oauth is not None:
        oauth.init_app(app)
        oauth.register(
            name="line",
            client_id=os.getenv("LINE_CHANNEL_ID") or "",
            client_secret=os.getenv("LINE_CHANNEL_SECRET") or "",
            server_metadata_url="https://access.line.me/.well-known/openid-configuration",
            authorize_url="https://access.line.me/oauth2/v2.1/authorize",
            api_base_url="https://api.line.me/",
            client_kwargs={"scope": "profile", "token_endpoint_auth_method": "client_secret_post"},
        )
    return True

# ─── 追加：講座判定ヘルパ ─────────────────────────────────────────────
def _is_lecture_event(ev: dict) -> bool:
    """タイトルが【講座】で始まるイベントを講座扱いにする"""
    try:
        return str(ev.get("title") or "").startswith("【講座】")
    except Exception:
        return False


def _lecture_prepaid_guard(ev: dict, me: dict, event_uuid: str):
    """
    講座イベントで未払いなら支払いページへ強制リダイレクトする。
    join画面へのGET/POSTのいずれも入口で使う。
    """
    if not _is_lecture_event(ev):
        return None  # 非講座は何もしない

    # 参加者の支払状況を確認
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT COALESCE(payment_status,'unpaid') AS ps
              FROM mfu_event_member
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (ev["id"], me["id"]))  # type: ignore
        row = cur.fetchone()
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    ps = ((row or {}).get("ps") or "unpaid").lower()
    if ps != "paid":
        flash("この講座は『先にお支払い』が必要です。お支払いページへ移動します。", "warning")
        return redirect(url_for("external_login_user.pay_start", event_uuid=event_uuid))
    return None

# ─── 追加：joinルート入口での一括ガード（差し込み用フック） ─────────────
@bp.before_app_request
def _enforce_lecture_prepaid_on_join():
    """
    /external-login/events/join/<uuid> に来たら、
    講座イベントで未払いの人は /pay/start/<uuid> へ飛ばす。
    ※ 未ログイン時はここでは何もしない（join本体で既存のログインガードが動く）
    """
    from flask import request, flash, redirect, url_for

    # 対象エンドポイントだけ
    if request.endpoint != "external_login_user.join_event":
        return None

    # URL から event_uuid
    event_uuid = (request.view_args or {}).get("event_uuid")
    if not event_uuid:
        return None

    ext_session = get_ext_session()
    iv = (request.args.get("iv") or "").strip()
    if iv:
        store = ext_session.get("lecture_invite_tokens") or {}
        store[event_uuid] = iv
        ext_session["lecture_invite_tokens"] = store

    # 未ログインならスルー（join本体に任せる）
    if not ext_session.get("ext_user_social_id"):
        return None

    # ヘルパ（イベント/ユーザ取得）を遅延インポート（所在差に対応）
    _event_by_uuid_str = _get_ext_user_by_social = None
    try:
        from .routes import _event_by_uuid_str as _ev_by_uuid
        from .routes import _get_ext_user_by_social as _get_user_by_social
        _event_by_uuid_str = _ev_by_uuid
        _get_ext_user_by_social = _get_user_by_social
    except Exception:
        try:
            from .payments import _event_by_uuid_str as _ev_by_uuid
            from .payments import _get_ext_user_by_social as _get_user_by_social
            _event_by_uuid_str = _ev_by_uuid
            _get_ext_user_by_social = _get_user_by_social
        except Exception:
            pass
    if not (_event_by_uuid_str and _get_ext_user_by_social):
        return None  # ヘルパが無い環境はスルー

    # イベントとユーザ取得
    ev = _event_by_uuid_str(event_uuid)
    if not ev:
        return None
    me = _get_ext_user_by_social(ext_session.get("ext_user_social_id"))  # type: ignore
    if not me:
        return None

    # 講座判定
    title = str(ev.get("title") or "")
    if not title.startswith("【講座】"):
        return None

    # --- get_db を多段トライで解決 ---
    get_db = None
    try:
        from app.utils import get_db as _g
        get_db = _g
    except Exception:
        try:
            from app.utils.db import get_db as _g
            get_db = _g
        except Exception:
            try:
                from ..utils import get_db as _g
                get_db = _g
            except Exception:
                try:
                    from ..utils.db import get_db as _g
                    get_db = _g
                except Exception:
                    try:
                        from app import get_db as _g
                        get_db = _g
                    except Exception:
                        get_db = None
    if get_db is None:
        # DB取得手段が無い環境は安全側でスルー（join本体で従来処理へ）
        return None

    # 支払状況チェック（未払いなら支払へ）
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("""
            SELECT COALESCE(payment_status,'unpaid') AS ps
              FROM mfu_event_member
             WHERE event_id=%s AND user_id=%s
             LIMIT 1
        """, (ev["id"], me["id"]))  # type: ignore
        row = cur.fetchone()
    finally:
        try:
            cur.close(); db.close()
        except Exception:
            pass

    ps = ((row or {}).get("ps") or "unpaid").lower()
    if ps != "paid":
        flash("この講座は『先にお支払い』が必要です。お支払いページへ移動します。", "warning")
        return redirect(url_for("external_login_user.pay_start", event_uuid=event_uuid))

    return None

# === 未確認メールのユーザーを専用ページへ誘導（全域ガード） ====================
from .utils import get_db  # 既存

_ALLOW_UNVERIFIED_ENDPOINTS = {
    # プロフィール編集、ログアウト、再送、確認リンク、未確認専用ページ、アバター配信
    "external_login_user.profile",
    "external_login_user.logout",
    "external_login_user.resend_verify_email",
    "external_login_user.latest_verify_email_status",
    "external_login_user.unverified",
    "external_login_user.avatar_file",   # ★ 追加：アバター画像の配信は許可
}

def _endpoint_allowed_when_unverified(ep: str | None) -> bool:
    if not ep:
        return False
    if ep in _ALLOW_UNVERIFIED_ENDPOINTS:
        return True
    if "verify" in ep:   # 例: verify_email 等
        return True
    if ep == "static":
        return True
    return False

def _is_email_unverified() -> bool:
    """email 登録済み かつ email_verified_at が NULL なら True（g にキャッシュ）"""
    if hasattr(g, "_ext_email_unverified"):
        return g._ext_email_unverified
    ext_session = get_ext_session()
    uid = ext_session.get("ext_user_id")
    if not uid:
        g._ext_email_unverified = False
        return False
    db = get_db(); cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT email, email_verified_at FROM external_login_user WHERE id=%s LIMIT 1", (uid,))
        row = cur.fetchone()
    finally:
        try: cur.close(); db.close()
        except Exception: pass
    email = (row.get("email") or "").strip() if row else ""
    g._ext_email_unverified = bool(email and row and not row.get("email_verified_at"))
    return g._ext_email_unverified

@bp.before_app_request
def _lock_unverified_globally():
    try:
        # 未ログインは対象外
        ext_session = get_ext_session()
        if not ext_session.get("ext_user_id"):
            return None
        # 未確認でなければ素通り
        if not _is_email_unverified():
            return None
        # 許可エンドポイントなら通す
        if _endpoint_allowed_when_unverified(request.endpoint):
            return None
        # 既に専用ページにいるなら何もしない
        if request.endpoint == "external_login_user.unverified":
            return None
        # next で戻れるよう保持して未確認ページへ
        return redirect(url_for("external_login_user.unverified", next=request.full_path or ""))
    except Exception:
        current_app.logger.exception("unverified global lock failed")
        return None
# ========================================================================


# --- サブモジュール読み込み（ルート登録） ---
from . import utils  # noqa: F401
from . import schema  # noqa: F401
from . import albums  # noqa: F401
from . import payments  # noqa: F401
from . import users  # noqa: F401
from . import admin  # noqa: F401
from . import admin_users  # ← 外部ログインユーザー管理ルートを有効化
