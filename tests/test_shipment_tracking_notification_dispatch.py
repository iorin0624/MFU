"""Shipment notifications must not depend on the Discord channel being enabled."""

import ast
from datetime import datetime
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "shipment_tracking" / "services.py"


class ShipmentTrackingError(Exception):
    pass


def load_function(name: str, namespace: dict) -> Any:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace[name]


class FakeDb:
    def __init__(self, target: dict):
        self.target = target
        self.commits = 0

    def cursor(self, **_kwargs):
        return self

    def execute(self, statement, _params=None):
        self.statement = statement

    def fetchone(self):
        return self.target

    def commit(self):
        self.commits += 1

    def close(self):
        pass


class ShipmentNotificationTests(unittest.TestCase):
    def test_scheduled_status_change_dispatches_without_discord_webhook(self):
        target = {
            "id": 31,
            "carrier_code": "sagawa",
            "tracking_number": "123456789012",
            "last_payload_json": '{}',
            "last_check_success_at": datetime(2026, 11, 21, 9, 50),
            "last_current_status": "集荷",
            "last_current_status_detail": "集荷しました",
            "last_latest_event_at": datetime(2026, 11, 20, 21, 35),
        }
        payload = {
            "current_status": "配達中",
            "current_status_detail": "配達中です",
            "latest_event_at": "2026-11-21 09:46:00",
            "completed": False,
        }
        db = FakeDb(target)
        sent = []
        namespace = {
            "TRIGGERED_BY_VALUES": {"initial", "manual", "scheduled"},
            "ShipmentTrackingError": ShipmentTrackingError,
            "get_db": lambda: db,
            "_now": lambda: datetime(2026, 11, 21, 10),
            "fetch_and_parse": lambda *_args: payload,
            "json": json,
            "_parse_payload_datetime": lambda value: datetime.strptime(value, "%Y-%m-%d %H:%M:%S"),
            "AUTO_DEACTIVATE_STATUSES": set(),
            "_build_shipment_tracking_discord_embed": lambda **_kwargs: {"title": "更新"},
            "_send_shipment_tracking_notification": sent.append,
            "current_app": SimpleNamespace(logger=SimpleNamespace(exception=lambda *_args: None)),
        }
        run_check = load_function("run_check", namespace)

        self.assertTrue(run_check(31, "scheduled"))
        self.assertEqual(sent, [{"title": "更新"}])
        self.assertEqual(db.commits, 1)

    def test_common_notification_succeeds_when_discord_is_disabled(self):
        calls = []
        module = ModuleType("app.utils.notification_service")

        def dispatch(feature_key, payload, **kwargs):
            calls.append((feature_key, payload, kwargs))
            return {"ok": True, "delivery": {"in_app": "created", "discord": "disabled"}}

        module.dispatch_discord_notification = dispatch
        namespace = {"Any": Any, "ShipmentTrackingError": ShipmentTrackingError}
        send = load_function("_send_shipment_tracking_notification", namespace)

        with patch.dict(sys.modules, {"app.utils.notification_service": module}):
            send({"title": "更新"})

        self.assertEqual(calls[0][0], "shipment_tracking")
        self.assertEqual(calls[0][1], {"embeds": [{"title": "更新"}]})
        self.assertNotIn("legacy_webhook", calls[0][2])


if __name__ == "__main__":
    unittest.main()
