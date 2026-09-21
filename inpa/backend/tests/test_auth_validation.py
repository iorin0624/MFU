import pytest

from inpa_app import create_app
from inpa_app.auth.service import RegistrationError, hash_secret, new_public_id, validate_password


@pytest.fixture
def app():
    return create_app(
        "public",
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "TOKEN_PEPPER": "test-pepper",
            "MAIL_ENCRYPTION_KEY": "c29tZV9ub3RfcmVhbF9mZXJuZXRfa2V5XzMyX2J5dGVzISE=",
        },
    )


def test_public_ids_use_the_documented_crockford_alphabet():
    public_id = new_public_id()

    assert len(public_id) == 26
    assert set(public_id) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_secret_hash_is_pepper_bound(app):
    with app.app_context():
        assert hash_secret("token") == hash_secret("token")
        assert hash_secret("token") != hash_secret("other-token")


def test_password_policy_requires_twelve_characters():
    with pytest.raises(RegistrationError):
        validate_password("too-short")
    assert validate_password("a-safe-password") == "a-safe-password"


def test_login_does_not_leak_invalid_json_details(app):
    response = app.test_client().post("/api/v1/auth/login", json=[])

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "login_failed"
