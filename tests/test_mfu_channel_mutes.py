"""Regression checks for MFU notification-center channel mutes."""

import ast
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "utils" / "notification_service.py"
NOTIFICATIONS = ROOT / "external_login_user" / "notifications.py"
TEMPLATE = ROOT / "external_login_user" / "template" / "notifications.html"


class FakeDb:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []
        self.committed = False

    def cursor(self, **_kwargs):
        return self

    def execute(self, sql, params=()):
        self.statements.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def commit(self):
        self.committed = True

    def close(self):
        pass


def load_service_functions(db, *names):
    tree = ast.parse(SERVICE.read_text(encoding="utf-8"))
    chosen = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        "FEATURE_DEFINITIONS": {"known": {}},
        "datetime": datetime,
        "Any": Any,
        "get_db": lambda: db,
        "ensure_notification_service_schema": lambda: None,
    }
    exec(compile(ast.Module(body=chosen, type_ignores=[]), str(SERVICE), "exec"), namespace)
    return namespace


def test_known_and_other_channels_use_effective_mute_state():
    db = FakeDb([
        {"feature_key": "*", "muted_forever": 1, "mute_until": None},
        {"feature_key": "known", "muted_forever": 0, "mute_until": None},
        {"feature_key": "other", "muted_forever": 0,
         "mute_until": datetime.utcnow() + timedelta(hours=1)},
    ])
    functions = load_service_functions(db, "_is_muted", "mfu_channel_mutes")
    assert functions["mfu_channel_mutes"]("admin") == {"known": False, "other": True}


def test_other_channel_takes_precedence_over_unknown_feature_preference():
    db = FakeDb([{"muted_forever": 1, "mute_until": None}])
    functions = load_service_functions(db, "_preference")
    assert functions["_preference"]("admin", "unclassified_feature")["muted_forever"] is True
    _sql, params = db.statements[0]
    assert params == ("admin", "other", "unclassified_feature", "other", "unclassified_feature")


def test_setting_channel_mute_does_not_mark_existing_notifications_read():
    db = FakeDb()
    functions = load_service_functions(db, "set_mfu_channel_mute")
    functions["mfu_channel_mutes"] = lambda _username: {"known": True}
    assert functions["set_mfu_channel_mute"]("admin", "known", True) == {"known": True}
    assert db.committed
    assert db.statements[0][1] == ("admin", "known", 1)
    assert "mfu_notifications" not in db.statements[0][0]
    try:
        functions["set_mfu_channel_mute"]("admin", "all", True)
    except ValueError:
        pass
    else:
        raise AssertionError("all must not be a mutable channel")


def test_center_exposes_authenticated_channel_mute_controls():
    api = NOTIFICATIONS.read_text(encoding="utf-8")
    html = TEMPLATE.read_text(encoding="utf-8")
    assert '@mfu_notifications_bp.route("/api/mfu-notifications/mutes", methods=["GET", "POST"])' in api
    assert "username, error = _require_mfu_admin_acl()" in api
    assert "type(payload.get(\"muted\")) is not bool" in api
    assert 'id="channelMuteBtn"' in html
    assert 'body:JSON.stringify({channel, muted:!mutedChannels.has(channel)})' in html


def test_legacy_discord_senders_honor_common_mute_decision():
    service = SERVICE.read_text(encoding="utf-8")
    assert 'return not result.get("muted")' in service
    for relative in (
        "bank_account/routes.py", "external_login_user/payments.py",
        "external_login_user/users.py", "invoice/services.py", "payment/__init__.py",
        "routes/mfa_routes.py", "signage/rain_alert.py", "signage/train_alert.py",
        "utils/admin_auth.py", "utils/logs.py", "utils/upload_notifications.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "if " in source and "mirror_discord_payload_best_effort(" in source, relative
