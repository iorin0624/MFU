"""LAN-only WSGI entry point for MFU Uploader.

This process reuses MFU's existing uploader authentication and upload
blueprints, but exposes only the endpoints required by the desktop uploader.
It is intentionally bound to a dedicated LAN port by systemd.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from werkzeug.wrappers import Response

from app import app as mfu_app


ALLOWED_ENDPOINTS = {
    ("GET", "/desktop/uploader/api/session"),
    ("POST", "/desktop/uploader/api/revoke"),
    ("GET", "/api/ext/up/modes"),
    ("POST", "/api/ext/up/create"),
    ("POST", "/api/ext/up/original"),
    ("POST", "/api/ext/up/thumb"),
    ("POST", "/api/ext/up/reconcile-thumbnails"),
    ("POST", "/api/ext/up/done"),
}


class LanUploaderGateway:
    """Restrict a WSGI application to the desktop uploader API surface."""

    def __init__(self, application: Callable) -> None:
        self.application = application

    def __call__(self, environ: dict, start_response: Callable):
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = str(environ.get("PATH_INFO") or "/")
        if (method, path) not in ALLOWED_ENDPOINTS:
            body = json.dumps(
                {"ok": False, "error": "not_available_on_lan_uploader"},
                ensure_ascii=False,
            )
            return Response(body, status=404, content_type="application/json; charset=utf-8")(
                environ, start_response
            )

        # This listener is reached directly from the LAN. Do not let a client
        # spoof proxy headers consumed by the main application's ProxyFix.
        for key in (
            "HTTP_FORWARDED",
            "HTTP_X_FORWARDED_FOR",
            "HTTP_X_FORWARDED_HOST",
            "HTTP_X_FORWARDED_PORT",
            "HTTP_X_FORWARDED_PREFIX",
            "HTTP_X_FORWARDED_PROTO",
        ):
            environ.pop(key, None)
        return self.application(environ, start_response)


application = LanUploaderGateway(mfu_app.wsgi_app)

