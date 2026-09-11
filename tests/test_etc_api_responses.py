from flask import Flask, jsonify

from app.etc_accounting.routes import login_required


def test_login_required_returns_json_for_api_request():
    application = Flask(__name__)
    application.secret_key = "test"

    @application.get("/api")
    @login_required
    def api():
        return jsonify({"ok": True})

    response = application.test_client().get("/api", headers={"Accept": "application/json"})
    assert response.status_code == 401
    assert response.is_json
    assert response.get_json()["ok"] is False
