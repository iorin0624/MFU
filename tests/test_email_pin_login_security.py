from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
USERS = (ROOT / "external_login_user" / "users.py").read_text(encoding="utf-8")
SCHEMA = (ROOT / "external_login_user" / "schema.py").read_text(encoding="utf-8")
EXT_UTILS = (ROOT / "external_login_user" / "utils.py").read_text(encoding="utf-8")


def test_pin_hash_uses_server_secret_hmac_and_keeps_short_migration_compatibility():
    assert "hmac.new(_email_pin_hmac_key()" in USERS
    assert 'f"mfu-email-pin:v1:{_normalize_login_email(email)}:{pin}"' in USERS
    assert "hmac.compare_digest(_hash_pin(pin, email), stored)" in USERS
    assert "_legacy_hash_pin(pin)" in USERS


def test_pin_failures_are_limited_by_email_and_ip_without_issue_reset():
    assert "EMAIL_PIN_MAX_FAILURES = 5" in USERS
    assert "EMAIL_PIN_LOCK_DURATION = timedelta(minutes=15)" in USERS
    assert '("email", _login_scope_hash' in USERS
    assert '("ip", _login_scope_hash' in USERS
    assert "SELECT failure_count, first_failed_at, last_failed_at, locked_until" in USERS
    issue_body = USERS.split("def _issue_pin", 1)[1].split("def _resolve_user_by_email", 1)[0]
    assert "_clear_pin_login_failures" not in issue_body


def test_external_email_is_unique_and_social_upsert_cannot_update_on_email_collision():
    assert "UNIQUE KEY uq_external_login_user_email (email)" in SCHEMA
    assert "CREATE UNIQUE INDEX uq_external_login_user_email" in SCHEMA
    upsert_body = EXT_UTILS.split("def _upsert_ext_user", 1)[1].split("def _update_profile", 1)[0]
    assert "WHERE social_id=%s" in upsert_body
    assert "ON DUPLICATE KEY UPDATE" not in upsert_body


def test_pin_login_success_and_failure_are_audited():
    assert "[PIN_LOGIN_FAILED]" in USERS
    assert '_write_login_log(target["id"]' in USERS
    assert '"PIN_LOGIN"' in USERS
