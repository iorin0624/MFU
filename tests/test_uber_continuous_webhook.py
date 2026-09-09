from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from app.records import bp
from app.records import uber_continuous_webhook as webhook


class FakeCursor:
    def __init__(self, fetchone=None, rowcount=1):
        self._fetchone = fetchone
        self.rowcount = rowcount
        self.executions = []

    def execute(self, sql, params=()):
        self.executions.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._fetchone


class FakeDb:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self, **kwargs):
        return self._cursor

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


class UberContinuousWebhookTest(unittest.TestCase):
    def test_issue_stores_only_hash_and_revokes_previous_token(self):
        cursor = FakeCursor()
        db = FakeDb(cursor)
        with patch.object(webhook, "get_db", return_value=db), patch.object(
            webhook.secrets, "token_urlsafe", return_value="fixed-secret"
        ):
            raw = webhook.issue_token("admin")

        self.assertEqual(raw, "uws_fixed-secret")
        self.assertTrue(db.committed)
        self.assertIn("revoked_at", cursor.executions[0][0])
        insert_params = cursor.executions[1][1]
        self.assertEqual(insert_params[0], hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotIn(raw, insert_params)

    def test_valid_token_updates_last_used_metadata(self):
        cursor = FakeCursor(fetchone={"id": 7})
        db = FakeDb(cursor)
        raw = "uws_valid-token"
        with patch.object(webhook, "get_db", return_value=db):
            accepted = webhook.authenticate_token(raw, "192.0.2.10")

        self.assertTrue(accepted)
        self.assertEqual(cursor.executions[0][1][0], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(cursor.executions[1][1][1], "192.0.2.10")
        self.assertTrue(db.committed)

    def test_malformed_token_is_rejected_without_database_lookup(self):
        with patch.object(webhook, "get_db") as get_db:
            self.assertFalse(webhook.authenticate_token("not-a-webhook-token", "192.0.2.10"))
        get_db.assert_not_called()

    def test_rate_limit_uses_ten_requests_per_window(self):
        cursor = FakeCursor(fetchone=(10,))
        with patch.object(webhook, "get_db", return_value=FakeDb(cursor)):
            self.assertTrue(webhook.is_rate_limited("192.0.2.10"))
        self.assertIn("action NOT IN", cursor.executions[0][0])

    def test_start_is_idempotent_when_monitoring_is_already_enabled(self):
        current = {"enabled": 1, "status": "monitoring"}
        with patch.object(bp, "get_continuous_fetch_state", return_value=current), patch.object(
            bp, "_start_uber_continuous_process"
        ) as start_process:
            status, _, state = bp._enable_uber_continuous_fetch()

        self.assertEqual(status, "already_started")
        self.assertIs(state, current)
        start_process.assert_not_called()

    def test_active_import_defers_first_webhook_fetch(self):
        updated = {"enabled": 1, "status": "monitoring"}
        with patch.object(bp, "get_continuous_fetch_state", return_value={"enabled": 0}), patch.object(
            bp, "get_active_import_job", return_value={"id": "busy"}
        ), patch.object(bp, "update_continuous_fetch_state", return_value=updated), patch.object(
            bp, "_start_uber_continuous_process"
        ) as start_process:
            status, _, state = bp._enable_uber_continuous_fetch()

        self.assertEqual(status, "started_deferred")
        self.assertIs(state, updated)
        start_process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
