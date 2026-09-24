import sqlite3

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from inpa_app import calendar, create_app


def _app(monkeypatch):
    app = create_app("public", {"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False, "detect_types": sqlite3.PARSE_DECLTYPES},
        poolclass=StaticPool,
    )
    app.extensions["inpa_db_engine"] = engine
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY,public_id TEXT,connection_id TEXT,"
            "display_name TEXT,x_handle TEXT,instagram_handle TEXT,x_handle_visible INTEGER,"
            "instagram_handle_visible INTEGER,status TEXT)"
        ))
        connection.execute(text(
            "CREATE TABLE seasons (id INTEGER PRIMARY KEY,public_id TEXT,name TEXT,start_date DATE,"
            "end_date DATE,is_active INTEGER)"
        ))
        connection.execute(text(
            "CREATE TABLE visits (id INTEGER PRIMARY KEY,user_id INTEGER,season_id INTEGER,"
            "visit_date DATE,park TEXT,costume TEXT,memo TEXT)"
        ))
        connection.execute(text("CREATE TABLE follows (follower_user_id INTEGER,followed_user_id INTEGER)"))
        connection.execute(text("CREATE TABLE blocks (blocker_user_id INTEGER,blocked_user_id INTEGER)"))
        connection.execute(text(
            "CREATE TABLE restriction_periods (id INTEGER PRIMARY KEY,public_id TEXT,name TEXT,"
            "restriction_type TEXT,start_date DATE,end_date DATE,park_scope TEXT,description TEXT,"
            "enforcement TEXT,season_id INTEGER,is_active INTEGER)"
        ))
        for table in ("user_privacy_settings", "user_season_privacy_settings"):
            season_column = "season_id INTEGER," if table.startswith("user_season") else ""
            connection.execute(text(
                f"CREATE TABLE {table} (user_id INTEGER,{season_column}audience TEXT,"
                "show_date INTEGER,show_park INTEGER,show_costume INTEGER,show_memo INTEGER)"
            ))
        connection.execute(text(
            "INSERT INTO users VALUES "
            "(1,'VIEWER','11112222','Viewer',NULL,NULL,0,0,'active'),"
            "(2,'TARGET','AAAABBBB','Target',NULL,NULL,0,0,'active')"
        ))
        connection.execute(text("INSERT INTO seasons VALUES (1,'SEASON','Season','2026-09-01','2026-10-31',1)"))
        connection.execute(text("INSERT INTO visits VALUES (1,2,1,'2026-10-01','land','Costume','Memo')"))
        connection.execute(text(
            "INSERT INTO user_privacy_settings VALUES "
            "(2,'link',0,0,0,0),(2,'logged_in',1,1,0,0),"
            "(2,'mutual',0,0,1,0),(2,'private',0,0,0,1)"
        ))
    monkeypatch.setattr(calendar, "_require_session", lambda: ({"user_id": 1}, None))
    return app


def test_unrelated_logged_in_user_can_view_only_logged_in_fields(monkeypatch):
    response = _app(monkeypatch).test_client().get(
        "/api/v1/calendar?year=2026&month=10&season_id=SEASON&person_id=AAAA-BBBB"
    )

    assert response.status_code == 200
    entry = response.get_json()["days"][0]["entries"][0]
    assert entry["park"] == "land"
    assert "costume" not in entry
    assert "memo" not in entry


def test_blocked_person_calendar_returns_no_entries(monkeypatch):
    app = _app(monkeypatch)
    with app.extensions["inpa_db_engine"].begin() as connection:
        connection.execute(text("INSERT INTO blocks VALUES (2,1)"))

    response = app.test_client().get(
        "/api/v1/calendar?year=2026&month=10&season_id=SEASON&person_id=AAAABBBB"
    )

    assert response.status_code == 200
    assert response.get_json()["days"] == []
