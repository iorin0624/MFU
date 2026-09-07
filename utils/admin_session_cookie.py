"""Per-account expiration rules for MFU's signed Flask session cookie."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask.sessions import SecureCookieSessionInterface


AUTHENTICATED_COOKIE_TTL = timedelta(days=7)
# Backward-compatible name for callers/tests that referenced the old constant.
ADMIN_COOKIE_TTL = AUTHENTICATED_COOKIE_TTL


class MFUSecureCookieSessionInterface(SecureCookieSessionInterface):
    """Use a rolling seven-day cookie for admin and external-user logins."""

    def get_expiration_time(self, app, session):
        is_admin = session.get("user") == "admin"
        is_external_user = bool(session.get("ext_user_id"))
        if session.permanent and (is_admin or is_external_user):
            return datetime.now(timezone.utc) + AUTHENTICATED_COOKIE_TTL
        return super().get_expiration_time(app, session)
