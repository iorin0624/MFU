import ast
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    path = ROOT / "utils" / "access_log_retention.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    tree.body = [
        node for node in tree.body
        if not (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").startswith("app.")
        )
    ]
    module = types.ModuleType("testable_access_log_retention")
    module.__dict__.update({"__file__": str(path), "get_db": lambda: None})
    sys.modules[module.__name__] = module
    exec(compile(tree, str(path), "exec"), module.__dict__)
    return module


retention = _load_module()


class AccessLogRetentionRuleTest(unittest.TestCase):
    def test_calendar_month_cutoff_clamps_month_end(self):
        source = datetime(2026, 8, 31, 12, 34, 56, tzinfo=retention.JST)
        self.assertEqual(
            retention.subtract_calendar_months(source, 6),
            datetime(2026, 2, 28, 12, 34, 56, tzinfo=retention.JST),
        )

    def test_sql_targets_structured_and_recognized_legacy_access_only(self):
        sql = retention.ACCESS_LOG_WHERE_SQL
        self.assertIn("COALESCE(method, '') <> ''", sql)
        self.assertIn("COALESCE(path, '') <> ''", sql)
        for method in retention.HTTP_METHODS:
            self.assertIn(f"log_text LIKE '{method} %'", sql)
        self.assertNotIn("[LOGIN]", sql)
        self.assertNotIn("[LINE_LOGIN]", sql)
        self.assertNotIn("[SMTP]", sql)


class FakeCursor:
    def __init__(self):
        self.rowcount = 0
        self._one = None
        self.statements = []

    def execute(self, sql, params):
        normalized = " ".join(sql.split())
        self.statements.append((normalized, params))
        if "GET_LOCK" in normalized:
            self._one = {"acquired": 1}
        elif normalized.startswith("DELETE FROM logs"):
            self.rowcount = 5000
        elif normalized.startswith("SELECT COUNT(*)"):
            self._one = {
                "matched": 304547,
                "oldest_matched": datetime(2025, 7, 2),
                "newest_matched": datetime(2026, 1, 13),
            }

    def fetchone(self):
        return self._one


class FakeDb:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.committed = False
        self.rolled_back = False

    def cursor(self, dictionary=False):
        return self.cursor_instance

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class AccessLogRetentionExecutionTest(unittest.TestCase):
    def test_dry_run_only_counts(self):
        db = FakeDb()
        result = retention.run_access_log_retention(
            dry_run=True,
            now=datetime(2026, 7, 13, 16, 0, tzinfo=retention.JST),
            db_factory=lambda: db,
        )
        self.assertEqual(result["matched"], 304547)
        self.assertEqual(result["deleted"], 0)
        self.assertFalse(any(sql.startswith("DELETE") for sql, _ in db.cursor_instance.statements))

    def test_live_run_deletes_one_oldest_first_limited_batch(self):
        db = FakeDb()
        result = retention.run_access_log_retention(
            now=datetime(2026, 7, 13, 16, 0, tzinfo=retention.JST),
            batch_size=5000,
            db_factory=lambda: db,
        )
        delete_sql, delete_params = next(
            (sql, params) for sql, params in db.cursor_instance.statements if sql.startswith("DELETE FROM logs")
        )
        self.assertEqual(result["deleted"], 5000)
        self.assertIn("ORDER BY log_date ASC, id ASC", delete_sql)
        self.assertIn("LIMIT %s", delete_sql)
        self.assertEqual(delete_params[-1], 5000)
        self.assertTrue(db.committed)


if __name__ == "__main__":
    unittest.main()
