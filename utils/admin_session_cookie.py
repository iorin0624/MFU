"""Per-account expiration rules for MFU's signed Flask session cookie."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask.sessions import SecureCookieSessionInterface


ADMIN_COOKIE_TTL = timedelta(days=7)


class MFUSecureCookieSessionInterface(SecureCookieSessionInterface):
    """Keep the normal 60-day lifetime while limiting admin cookies to 7 days."""

    def get_expiration_time(self, app, session):
        if session.permanent and session.get("user") == "admin":
            return datetime.now(timezone.utc) + ADMIN_COOKIE_TTL
        return super().get_expiration_time(app, session)
