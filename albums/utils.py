# -*- coding: utf-8 -*-
from __future__ import annotations

from app.external_login_user.ext_session import get_ext_session
from app.external_login_user.utils import _get_ext_user_by_social, sanitize_next_path


def get_current_ext_user_id() -> int | None:
    """外部ログインユーザーIDを取得する（ext_session 優先）。"""
    ext_session = get_ext_session()
    ext_user_id = ext_session.get("ext_user_id")
    if ext_user_id:
        try:
            return int(ext_user_id)
        except (TypeError, ValueError):
            return None
    ext_social_id = ext_session.get("ext_user_social_id")
    if not ext_social_id:
        return None
    try:
        ext_user = _get_ext_user_by_social(ext_social_id)
    except Exception:
        return None
    if not ext_user:
        return None
    try:
        return int(ext_user.get("id"))
    except (TypeError, ValueError):
        return None


def set_ext_after_login_next(raw_next: str) -> str:
    """ログイン後遷移先を安全に保存し、旧キーはクリアする。"""
    ext_session = get_ext_session()
    local_next = sanitize_next_path(raw_next)
    ext_session["ext_after_login_next"] = local_next
    if ext_session.get("after_login_redirect"):
        ext_session.pop("after_login_redirect", None)
    return local_next


def migrate_after_login_redirect() -> str | None:
    """旧キー after_login_redirect が存在すれば新キーへ移行する。"""
    ext_session = get_ext_session()
    if ext_session.get("ext_after_login_next"):
        if ext_session.get("after_login_redirect"):
            ext_session.pop("after_login_redirect", None)
        return ext_session.get("ext_after_login_next")
    legacy = ext_session.get("after_login_redirect")
    if not legacy:
        return None
    local_next = sanitize_next_path(legacy)
    ext_session["ext_after_login_next"] = local_next
    ext_session.pop("after_login_redirect", None)
    return local_next
