from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from inpa_app import create_app, relationships


def _app(monkeypatch):
    app = create_app(
        "public",
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "TOKEN_PEPPER": "test-pepper",
        },
    )
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    app.extensions["inpa_db_engine"] = engine
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, public_id TEXT UNIQUE, "
            "connection_id TEXT UNIQUE, display_name TEXT, x_handle TEXT, instagram_handle TEXT, "
            "x_handle_visible INTEGER, instagram_handle_visible INTEGER, status TEXT)"
        ))
        connection.execute(text(
            "CREATE TABLE follows (follower_user_id INTEGER, followed_user_id INTEGER, "
            "PRIMARY KEY (follower_user_id, followed_user_id))"
        ))
        connection.execute(text(
            "CREATE TABLE blocks (blocker_user_id INTEGER, blocked_user_id INTEGER, "
            "PRIMARY KEY (blocker_user_id, blocked_user_id))"
        ))
        connection.execute(text(
            "CREATE TABLE rate_limit_counters (bucket_key TEXT, action_name TEXT, "
            "window_started_at DATETIME, window_seconds INTEGER, request_count INTEGER, "
            "PRIMARY KEY (bucket_key, action_name, window_started_at))"
        ))
        connection.execute(text(
            "INSERT INTO users VALUES "
            "(1,'OWNERPUBLICID00000000000001','11112222','Owner',NULL,NULL,0,0,'active'),"
            "(2,'TARGETPUBLICID0000000000002','7K3MP9QX','Guest',NULL,NULL,0,0,'active')"
        ))
    monkeypatch.setattr(
        relationships,
        "_require_session",
        lambda: ({"user_id": 1, "csrf_secret_hash": "unused"}, None),
    )
    return app


def test_connection_lookup_accepts_lowercase_and_display_hyphen(monkeypatch):
    response = _app(monkeypatch).test_client().get(
        "/api/v1/people/by-connection-id/7k3m-p9qx"
    )

    assert response.status_code == 200
    assert response.get_json()["person"] == {
        "public_id": "TARGETPUBLICID0000000000002",
        "connection_id": "7K3MP9QX",
        "display_name": "Guest",
        "following": False,
        "follows_me": False,
    }


def test_connection_lookup_is_rate_limited(monkeypatch):
    client = _app(monkeypatch).test_client()

    for _ in range(10):
        assert client.get("/api/v1/people/by-connection-id/7K3MP9QX").status_code == 200
    response = client.get("/api/v1/people/by-connection-id/7K3MP9QX")

    assert response.status_code == 429
    assert response.get_json()["error"]["code"] == "rate_limited"
