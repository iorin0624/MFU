import importlib.util
import sqlite3
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


class ReplyLibraryTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parents[1] / "image_viewer" / "reply_library.py"
        spec = importlib.util.spec_from_file_location("mfu_reply_library_test", source)
        self.library = importlib.util.module_from_spec(spec)
        db_module = ModuleType("app.utils.db")
        db_module.get_db = lambda: self
        with patch.dict(sys.modules, {"app.utils.db": db_module}):
            spec.loader.exec_module(self.library)
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE uploads (id INTEGER, uuid TEXT, title TEXT, date TEXT,
                                  username TEXT, layer_deleted_at TEXT, upload_deleted_at TEXT);
            CREATE TABLE layer_upload_replies (id INTEGER, upload_id INTEGER, reply_uuid TEXT, posted_at TEXT);
            CREATE TABLE layer_upload_reply_files (id INTEGER, reply_id INTEGER, file_kind TEXT, filename TEXT, sort_order INTEGER);
            INSERT INTO uploads VALUES
                (1,'parent1','Ｄハロお疲れ様会','2026-11-01','owner',NULL,'2026-11-04'),
                (2,'parent2','別の親',NULL,'another',NULL,NULL),
                (3,'parent3','削除済','2026-11-03','owner','2026-11-10',NULL),
                (4,'parent4','空','2026-11-03','owner',NULL,NULL);
            INSERT INTO layer_upload_replies VALUES
                (10,1,'reply10','2026-11-08 10:00:00'),(11,1,'reply11','2026-11-09 10:00:00'),
                (12,1,'reply12','2026-11-09 11:00:00'),(20,2,'reply20','2026-11-09 09:00:00'),
                (30,3,'reply30','2026-11-10 12:00:00'),(40,4,'reply40','2026-11-11 12:00:00');
            INSERT INTO layer_upload_reply_files VALUES
                (101,10,'image','z.jpg',0),(102,10,'image','a.jpg',1),(111,11,'image','b.jpg',0),
                (121,12,'image','c.jpg',0),(201,20,'image','z.jpg',0),(301,30,'image','deleted.jpg',0),
                (401,40,'zip','ignore.zip',0);
        """)
        self.library.url_for = lambda endpoint, **parts: f'/{endpoint}/{parts["uuid"]}/{parts["reply_uuid"]}/{parts["filename"]}'

    def cursor(self, dictionary=False):
        return DatabaseCursor(self.db.cursor())

    def close(self):
        # The real pool returns the connection; keep the test fixture alive.
        pass

    def listing(self, folder="..", owner="owner", grouping="none", sort="asc", center=None, limit=25):
        return self.library.list_payload(owner, folder, sort=sort, page=1, per_page=limit, center=center, group_by=grouping)

    def test_parent_scope_honors_upload_history_permissions(self):
        self.assertEqual(self.listing()["replyFolders"], {"..": "加工バック", "../1": "2026年11月01日　Ｄハロお疲れ様会"})
        with self.assertRaises(LookupError):
            self.listing("../2")
        for folder in ("../3", "../4"):
            with self.assertRaises(LookupError):
                self.listing(folder, owner="admin")
        with self.assertRaises(ValueError):
            self.listing("../../1")
        self.assertEqual(self.listing(owner="admin")["replyFolders"]["../2"], "撮影日未設定　別の親")

    def test_sort_uses_reply_datetime_and_preserves_file_order(self):
        result = self.listing("../1", sort="desc")
        self.assertEqual([item["name"] for item in result["images"]], ["c.jpg", "b.jpg", "z.jpg", "a.jpg"])
        self.assertTrue(result["images"][0]["readOnly"])
        self.assertIn("layer_upload_thumbnail", result["images"][0]["thumbUrl"])
        self.assertEqual(result["version"], self.library.version("owner", "../1"))

    def test_groups_use_latest_reply_and_have_full_index_ranges(self):
        result = self.listing(owner="admin", grouping="replyParent", sort="desc", center=3, limit=2)
        self.assertEqual([(g["key"], g["start"], g["count"]) for g in result["groups"]], [("1", 0, 4), ("2", 4, 1)])
        self.assertEqual(result["pagination"]["offset"], 2)
        groups = self.listing("../1", grouping="replyUploaded", sort="desc")["groups"]
        self.assertEqual([(g["label"], g["count"]) for g in groups], [("2026年11月09日", 2), ("2026年11月08日", 2)])
        with self.assertRaises(ValueError):
            self.listing("../1", grouping="replyParent")


class DatabaseCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def execute(self, sql, parameters):
        self.cursor.execute(sql.replace("%s", "?"), parameters)

    def fetchall(self):
        rows = [dict(row) for row in self.cursor.fetchall()]
        for row in rows:
            for key in ("latest_posted", "posted_at"):
                if key in row:
                    row[key] = datetime.fromisoformat(row[key])
        return rows

    def close(self):
        self.cursor.close()


if __name__ == "__main__":
    unittest.main()
