# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from typing import Any

from flask import current_app, request, g
from itsdangerous import URLSafeTimedSerializer, BadSignature


EXTERNAL_SESSION_COOKIE_NAME = "mfu_event_session"
EXTERNAL_SESSION_COOKIE_PATHS = ("/external-login", "/e")
EXTERNAL_SESSION_SALT = "mfu-external-session"


class ExternalSession:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(data or {})
        self.modified = False

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.modified = True

    def pop(self, key: str, default: Any = None) -> Any:
        if key in self._data:
            self.modified = True
        return self._data.pop(key, default)

    def clear(self) -> None:
        if self._data:
            self.modified = True
        self._data.clear()

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)


def _external_secret_key() -> str:
    secret = current_app.config.get("EXTERNAL_SECRET_KEY") or os.environ.get("EXTERNAL_SECRET_KEY")
    if not secret:
        secret = current_app.config.get("SECRET_KEY") or ""
        if secret:
            current_app.logger.warning(
                "EXTERNAL_SECRET_KEY is not set; falling back to SECRET_KEY for external session signing."
            )
    return secret


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_external_secret_key(), salt=EXTERNAL_SESSION_SALT, serializer=json)


def load_ext_session(req=None) -> ExternalSession:
    req = req or request
    raw = req.cookies.get(EXTERNAL_SESSION_COOKIE_NAME)
    if not raw:
        return ExternalSession()
    try:
        data = _serializer().loads(raw)
    except BadSignature:
        return ExternalSession()
    if not isinstance(data, dict):
        return ExternalSession()
    return ExternalSession(data)


def get_ext_session() -> ExternalSession:
    sess = getattr(g, "ext_session", None)
    if sess is None:
        sess = load_ext_session()
        g.ext_session = sess
    return sess


def save_ext_session(response, ext_session: ExternalSession):
    if ext_session is None or not ext_session.modified:
        return response

    data = ext_session.to_dict()
    if not data:
        return clear_ext_session(response)

    value = _serializer().dumps(data)
    secure = current_app.config.get("EXTERNAL_SESSION_COOKIE_SECURE")
    if secure is None:
        secure = current_app.config.get("SESSION_COOKIE_SECURE", False)
    samesite = current_app.config.get("EXTERNAL_SESSION_COOKIE_SAMESITE") or current_app.config.get(
        "SESSION_COOKIE_SAMESITE", "Lax"
    )
    for path in EXTERNAL_SESSION_COOKIE_PATHS:
        response.set_cookie(
            EXTERNAL_SESSION_COOKIE_NAME,
            value,
            httponly=True,
            secure=secure,
            samesite=samesite,
            path=path,
        )
    return response


def clear_ext_session(response):
    for path in EXTERNAL_SESSION_COOKIE_PATHS:
        response.delete_cookie(EXTERNAL_SESSION_COOKIE_NAME, path=path)
    return response
