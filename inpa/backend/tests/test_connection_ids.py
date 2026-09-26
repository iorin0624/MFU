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
        connection.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, public_id TEXT UNIQUE, "
                "connection_id TEXT UNIQUE, display_name TEXT, x_handle TEXT, x_handle_normalized TEXT, "
                "instagram_handle TEXT, instagram_handle_normalized TEXT, "
                "x_handle_visible INTEGER, instagram_handle_visible INTEGER, status TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE follows (follower_user_id INTEGER, followed_user_id INTEGER, "
                "PRIMARY KEY (follower_user_id, followed_user_id))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE blocks (blocker_user_id INTEGER, blocked_user_id INTEGER, "
                "PRIMARY KEY (blocker_user_id, blocked_user_id))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE rate_limit_counters (bucket_key TEXT, action_name TEXT, "
                "window_started_at DATETIME, window_seconds INTEGER, request_count INTEGER, "
                "PRIMARY KEY (bucket_key, action_name, window_started_at))"
            )
        )
        connection.execute(
            text(
                "INSERT INTO users VALUES "
                "(1,'OWNERPUBLICID00000000000001','11112222','Owner',NULL,NULL,NULL,NULL,0,0,'active'),"
                "(2,'TARGETPUBLICID0000000000002','7K3MP9QX','Guest',NULL,NULL,NULL,NULL,0,0,'active'),"
                "(3,'XPUBLICID000000000000000003','2K3MP9QX','X Guest','Magic_User','magic_user',"
                "'hidden.insta','hidden.insta',1,0,'active'),"
                "(4,'INSTAPUBLICID00000000000004','3K3MP9QX','Insta Guest','Hidden_X','hidden_x',"
                "'magic_user','magic_user',0,1,'active')"
            )
        )
    monkeypatch.setattr(
        relationships,
        "_require_session",
        lambda: ({"user_id": 1, "csrf_secret_hash": "unused"}, None),
    )
    return app


def test_connection_lookup_accepts_lowercase_and_display_hyphen(monkeypatch):
    response = _app(monkeypatch).test_client().get("/api/v1/people/by-connection-id/7k3m-p9qx")

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


def test_search_finds_visible_social_ids_case_insensitively_and_allows_at_prefix(monkeypatch):
    response = _app(monkeypatch).test_client().get("/api/v1/people/search?q=%40MAGIC_USER")

    assert response.status_code == 200
    people = response.get_json()["people"]
    assert [person["display_name"] for person in people] == ["X Guest", "Insta Guest"]
    assert people[0]["x_handle"] == "Magic_User"
    assert "instagram_handle" not in people[0]
    assert people[1]["instagram_handle"] == "magic_user"
    assert "x_handle" not in people[1]


def test_search_does_not_find_hidden_social_ids_or_partial_matches(monkeypatch):
    client = _app(monkeypatch).test_client()

    hidden_x = client.get("/api/v1/people/search?q=hidden_x")
    hidden_instagram = client.get("/api/v1/people/search?q=hidden.insta")
    partial = client.get("/api/v1/people/search?q=magic")

    assert hidden_x.status_code == 200
    assert hidden_x.get_json()["people"] == []
    assert hidden_instagram.status_code == 200
    assert hidden_instagram.get_json()["people"] == []
    assert partial.status_code == 200
    assert partial.get_json()["people"] == []


def test_search_excludes_users_blocked_in_either_direction(monkeypatch):
    app = _app(monkeypatch)
    with app.extensions["inpa_db_engine"].begin() as connection:
        connection.execute(
            text("INSERT INTO blocks (blocker_user_id,blocked_user_id) VALUES (3,1)")
        )

    response = app.test_client().get("/api/v1/people/search?q=magic_user")

    assert response.status_code == 200
    assert [person["display_name"] for person in response.get_json()["people"]] == ["Insta Guest"]


def test_followers_lists_people_who_registered_the_current_user(monkeypatch):
    app = _app(monkeypatch)
    with app.extensions["inpa_db_engine"].begin() as connection:
        connection.execute(
            text("INSERT INTO follows (follower_user_id,followed_user_id) VALUES (2,1)")
        )

    response = app.test_client().get("/api/v1/followers")

    assert response.status_code == 200
    assert response.get_json()["people"] == [
        {
            "public_id": "TARGETPUBLICID0000000000002",
            "connection_id": "7K3MP9QX",
            "display_name": "Guest",
            "mutual": False,
            "following": False,
            "follows_me": True,
        }
    ]
