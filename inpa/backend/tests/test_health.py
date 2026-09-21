from inpa_app import create_app


def test_live_check_does_not_require_database() -> None:
    app = create_app("public", {"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    response = app.test_client().get("/health/live")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_internal_routes_are_not_registered_in_public_app() -> None:
    app = create_app("public", {"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    response = app.test_client().get("/internal/admin/v1/bootstrap-status")

    assert response.status_code == 404


def test_internal_bootstrap_route_is_available_only_in_admin_app() -> None:
    app = create_app("admin", {"TESTING": True, "DATABASE_URL": "sqlite+pysqlite:///:memory:"})
    response = app.test_client().get("/internal/admin/v1/bootstrap-status")

    assert response.status_code == 200
