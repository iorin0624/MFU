"""Configuration loading with a narrow, explicit environment-variable surface."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from flask import Flask


def _required(name: str, *, production: bool) -> str:
    value = os.environ.get(name, "")
    if production and (not value or value == "CHANGE_ME"):
        raise RuntimeError(f"{name} must be set in production")
    return value


def apply_settings(
    app: Flask, *, role: str, overrides: Mapping[str, Any] | None = None
) -> None:
    environment = os.environ.get("INPA_ENV", "development")
    production = environment == "production"
    root_dir = Path(__file__).resolve().parents[2]
    session_secret = _required("INPA_SESSION_SECRET", production=production)

    app.config.from_mapping(
        APP_ROLE=role,
        ENVIRONMENT=environment,
        PUBLIC_ORIGIN=os.environ.get("INPA_PUBLIC_ORIGIN", "http://localhost:5173"),
        DATABASE_URL=_required("INPA_DATABASE_URL", production=production),
        SECRET_KEY=session_secret,
        TOKEN_PEPPER=_required("INPA_TOKEN_PEPPER", production=production),
        MAIL_ENCRYPTION_KEY=_required("INPA_MAIL_ENCRYPTION_KEY", production=production),
        INTERNAL_ADMIN_HMAC_SECRET=_required("INPA_INTERNAL_ADMIN_HMAC_SECRET", production=production),
        MFU_NOTIFICATION_HOST=os.environ.get("INPA_MFU_NOTIFICATION_HOST", "127.0.0.1"),
        MFU_NOTIFICATION_PORT=int(os.environ.get("INPA_MFU_NOTIFICATION_PORT", "8080")),
        TURNSTILE_SECRET_KEY=_required("INPA_TURNSTILE_SECRET_KEY", production=production),
        TURNSTILE_VERIFY_URL="https://challenges.cloudflare.com/turnstile/v0/siteverify",
        TURNSTILE_BYPASS=not production and os.environ.get("INPA_TURNSTILE_BYPASS") == "1",
        MAIL_FROM=os.environ.get("INPA_MAIL_FROM", "noreply@localhost"),
        SMTP_HOST=os.environ.get("INPA_SMTP_HOST", ""),
        SMTP_PORT=int(os.environ.get("INPA_SMTP_PORT", "587")),
        SMTP_USERNAME=os.environ.get("INPA_SMTP_USERNAME", ""),
        SMTP_PASSWORD=os.environ.get("INPA_SMTP_PASSWORD", ""),
        SESSION_COOKIE_NAME=os.environ.get("INPA_SESSION_COOKIE_NAME", "inpa_session"),
        CSRF_COOKIE_NAME=os.environ.get("INPA_CSRF_COOKIE_NAME", "inpa_csrf"),
        REGISTRATION_TTL_SECONDS=86400,
        SESSION_IDLE_DAYS=30,
        SESSION_ABSOLUTE_DAYS=90,
        FRONTEND_DIST=root_dir / "frontend" / "dist",
        JSON_SORT_KEYS=False,
    )
    if overrides:
        app.config.update(overrides)
