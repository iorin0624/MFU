from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from inpa_app import create_app, planner


def _app(monkeypatch):
    app = create_app("public", {"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    app.extensions["inpa_db_engine"] = engine
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY,onboarding_step TEXT NOT NULL)"
        ))
        connection.execute(text("INSERT INTO users VALUES (1,'privacy')"))
    monkeypatch.setattr(planner, "_require_session", lambda: ({"user_id": 1}, None))
    monkeypatch.setattr(planner, "verify_csrf", lambda _session, _token: True)
    return app


def test_onboarding_progress_is_persisted_and_ordered(monkeypatch):
    client = _app(monkeypatch).test_client()

    assert client.get("/api/v1/onboarding").get_json() == {
        "step": "privacy", "required": True,
    }
    assert client.patch("/api/v1/onboarding", json={"step": "completed"}).status_code == 409
    assert client.patch("/api/v1/onboarding", json={"step": "visit"}).get_json() == {
        "step": "visit", "required": True,
    }
    assert client.patch("/api/v1/onboarding", json={"step": "completed"}).get_json() == {
        "step": "completed", "required": False,
    }
    assert client.get("/api/v1/onboarding").get_json()["required"] is False
