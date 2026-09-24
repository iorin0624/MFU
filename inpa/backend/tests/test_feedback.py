from sqlalchemy import text

from inpa_app import create_app, feedback
from inpa_app.db import get_engine


def _app():
    app = create_app("public", {
        "TESTING": True,
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
    })
    with app.app_context(), get_engine().begin() as connection:
        connection.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, public_id TEXT NOT NULL, connection_id TEXT NOT NULL, "
            "display_name TEXT NOT NULL, email_normalized TEXT NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE feedbacks (id INTEGER PRIMARY KEY AUTOINCREMENT, public_id TEXT NOT NULL, user_id INTEGER NOT NULL, "
            "sender_public_id TEXT NOT NULL, sender_connection_id TEXT NOT NULL, sender_display_name TEXT NOT NULL, "
            "sender_email TEXT NOT NULL, category TEXT NOT NULL, message TEXT NOT NULL, source_path TEXT, user_agent TEXT, "
            "status TEXT NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO users VALUES (1,'01USERPUBLICID000000000000','ABCDEFGH','テスト利用者','user@example.test')"
        ))
    return app


def _authenticated(monkeypatch):
    monkeypatch.setattr(feedback, "_require_session", lambda: ({"user_id": 1}, None))
    monkeypatch.setattr(feedback, "verify_csrf", lambda session, token: True)


def test_feedback_uses_server_side_sender_snapshot(monkeypatch):
    app = _app(); _authenticated(monkeypatch)
    response = app.test_client().post("/api/v1/feedback", json={
        "category": "feature", "message": "新しい機能を追加してほしいです。", "source_path": "/calendar?month=10",
        "sender_email": "spoof@example.test",
    })
    assert response.status_code == 201
    with app.app_context(), get_engine().connect() as connection:
        row = connection.execute(text(
            "SELECT sender_display_name,sender_email,sender_connection_id,category,source_path,status FROM feedbacks"
        )).mappings().one()
    assert row["sender_display_name"] == "テスト利用者"
    assert row["sender_email"] == "user@example.test"
    assert row["sender_connection_id"] == "ABCDEFGH"
    assert row["category"] == "feature"
    assert row["source_path"] == "/calendar?month=10"
    assert row["status"] == "new"


def test_feedback_rate_limit_is_three_per_five_minutes(monkeypatch):
    app = _app(); _authenticated(monkeypatch); client = app.test_client()
    payload = {"category": "bug", "message": "エラーが繰り返し表示されます。", "source_path": "/"}
    assert [client.post("/api/v1/feedback", json=payload).status_code for _ in range(3)] == [201, 201, 201]
    response = client.post("/api/v1/feedback", json=payload)
    assert response.status_code == 429
    assert response.get_json()["error"]["code"] == "feedback_rate_limited"


def test_feedback_rejects_external_source_and_short_message(monkeypatch):
    app = _app(); _authenticated(monkeypatch); client = app.test_client()
    assert client.post("/api/v1/feedback", json={
        "category": "other", "message": "短い", "source_path": "/",
    }).status_code == 400
    assert client.post("/api/v1/feedback", json={
        "category": "other", "message": "十分な長さを持つフィードバックです。", "source_path": "https://evil.test/",
    }).status_code == 400
