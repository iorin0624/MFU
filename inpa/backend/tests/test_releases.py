from sqlalchemy import text

from inpa_app import create_app, releases
from inpa_app.db import get_engine


def _app():
    app = create_app("public", {"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    with app.app_context(), get_engine().begin() as connection:
        connection.execute(text(
            "CREATE TABLE app_releases (id INTEGER PRIMARY KEY,public_id TEXT,version TEXT,"
            "version_major INTEGER,version_minor INTEGER,version_patch INTEGER,change_type TEXT,"
            "title TEXT,content_markdown TEXT,status TEXT,published_at DATETIME,created_at DATETIME,updated_at DATETIME)"
        ))
        connection.execute(text(
            "CREATE TABLE user_release_dismissals (user_id INTEGER,release_id INTEGER,dismissed_at DATETIME,"
            "PRIMARY KEY(user_id,release_id))"
        ))
        connection.execute(text(
            "INSERT INTO app_releases VALUES "
            "(1,'REL100','1.0.0',1,0,0,'major','Initial','First','published','2026-09-20 00:00:00','2026-09-20 00:00:00','2026-09-20 00:00:00'),"
            "(2,'REL110','1.1.0',1,1,0,'feature','Updates','Second','published','2026-09-24 15:00:00','2026-09-24 15:00:00','2026-09-24 15:00:00')"
        ))
    return app


def test_public_releases_are_newest_first_and_jst():
    app = _app()
    response = app.test_client().get("/api/v1/releases")
    assert response.status_code == 200
    data = response.get_json()
    assert data["current_version"] == "1.1.0"
    assert [item["version"] for item in data["releases"]] == ["1.1.0", "1.0.0"]
    assert data["releases"][0]["published_at"] == "2026-09-25T00:00:00+09:00"


def test_dismiss_latest_marks_all_older_releases(monkeypatch):
    app = _app()
    monkeypatch.setattr(releases, "_require_session", lambda: ({"user_id": 7}, None))
    monkeypatch.setattr(releases, "verify_csrf", lambda session, token: True)
    client = app.test_client()
    assert client.get("/api/v1/releases/unseen").get_json()["unseen_count"] == 2
    assert client.post("/api/v1/releases/REL110/dismiss").status_code == 204
    data = client.get("/api/v1/releases/unseen").get_json()
    assert data == {"release": None, "unseen_count": 0}
