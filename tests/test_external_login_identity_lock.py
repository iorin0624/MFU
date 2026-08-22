import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "external_login_user" / "identity_lock.py"
SPEC = importlib.util.spec_from_file_location("external_login_identity_lock", MODULE_PATH)
identity_lock = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(identity_lock)

is_deleted_identity_locked = identity_lock.is_deleted_identity_locked
get_deleted_identity_lock = identity_lock.get_deleted_identity_lock
lock_deleted_identity = identity_lock.lock_deleted_identity
social_identity_hash = identity_lock.social_identity_hash


class FakeCursor:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchone(self):
        return self.row


class DeletedIdentityLockTest(unittest.TestCase):
    def test_hash_is_stable_and_provider_scoped(self):
        line_hash = social_identity_hash("LINE", "U123")
        self.assertEqual(line_hash, social_identity_hash("line", " U123 "))
        self.assertNotEqual(line_hash, social_identity_hash("other", "U123"))
        self.assertEqual(len(line_hash), 64)

    def test_lock_stores_hash_not_raw_social_id(self):
        cur = FakeCursor()
        digest = lock_deleted_identity(
            cur,
            provider="line",
            social_id="U-secret-social-id",
            user_id=42,
            deleted_by="admin",
            reason="requested",
        )
        _, params = cur.calls[0]
        self.assertEqual(params[0], "line")
        self.assertEqual(params[1], digest)
        self.assertNotIn("U-secret-social-id", params)
        self.assertEqual(params[2], 42)

    def test_lookup_returns_cursor_result(self):
        locked = FakeCursor(row=(42,))
        self.assertTrue(is_deleted_identity_locked(locked, provider="line", social_id="U123"))
        unlocked = FakeCursor(row=None)
        self.assertFalse(is_deleted_identity_locked(unlocked, provider="line", social_id="U456"))

    def test_lookup_returns_original_user_id(self):
        locked = FakeCursor(row=(42,))
        self.assertEqual(
            get_deleted_identity_lock(locked, provider="line", social_id="U123"),
            (True, 42),
        )
        unlocked = FakeCursor(row=None)
        self.assertEqual(
            get_deleted_identity_lock(unlocked, provider="line", social_id="U456"),
            (False, None),
        )


if __name__ == "__main__":
    unittest.main()
