import importlib.util
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "image_viewer"
PACKAGE = "_image_style_jobs_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
for name in ("style_tags", "style_jobs"):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", ROOT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
jobs = sys.modules[f"{PACKAGE}.style_jobs"]


class StyleJobsTest(unittest.TestCase):
    def test_claim_owner_and_reviewable_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "jobs.sqlite3"
            created = jobs.create("alice", "folder/image.jpg", "a" * 64, db)
            self.assertEqual(created["status"], "pending")
            self.assertIsNone(jobs.claim("bob", 5, db))
            claimed = jobs.claim("alice", 8, db)
            self.assertEqual(claimed["id"], created["id"])
            self.assertIsNone(jobs.claimed_row(created["id"], "alice", 9, db))
            self.assertFalse(jobs.complete(created["id"], "alice", 9, {
                "assignment": {"major": "jirai", "middleTags": []},
                "scores": {"lolita": 0.1, "jirai": 0.8, "casual": 0.1},
            }, db))
            self.assertTrue(jobs.complete(created["id"], "alice", 8, {
                "assignment": {"major": "jirai", "middleTags": [{"name": "地雷系", "smallTags": []}]},
                "scores": {"lolita": 0.1, "jirai": 0.8, "casual": 0.1},
                "model": "test",
            }, db))
            result = jobs.get(created["id"], "alice", db)
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["suggestion"]["assignment"]["major"], "jirai")
            self.assertIsNone(jobs.get(created["id"], "bob", db))
            self.assertIsNone(jobs.claim("alice", 8, db))
            self.assertEqual(jobs.statuses([created["id"]], "alice", db)[0]["status"], "done")
            self.assertEqual(jobs.statuses([created["id"]], "bob", db), [])

    def test_invalid_result_does_not_finish_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "jobs.sqlite3"
            created = jobs.create("alice", "photo.png", "b" * 64, db)
            jobs.claim("alice", 2, db)
            with self.assertRaises(ValueError):
                jobs.complete(created["id"], "alice", 2, {
                    "assignment": {"major": "invalid", "middleTags": []},
                    "scores": {"lolita": 0.2, "jirai": 0.2, "casual": 0.6},
                }, db)
            self.assertEqual(jobs.get(created["id"], "alice", db)["status"], "running")

    def test_recent_recovers_running_job_and_scopes_to_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "jobs.sqlite3"
            old = jobs.create("alice", "old.jpg", "a" * 64, db)
            jobs.create("bob", "private.jpg", "b" * 64, db)
            with closing(sqlite3.connect(db)) as conn:
                with conn:
                    conn.execute("UPDATE image_style_jobs SET created_at=? WHERE id=?",
                                 (int(time.time()) - 7200, old["id"]))
            found = jobs.recent("alice", db)
            self.assertEqual([item["id"] for item in found], [old["id"]])
            self.assertEqual(found[0]["status"], "pending")
            self.assertIsNone(found[0]["suggestion"])


if __name__ == "__main__":
    unittest.main()
