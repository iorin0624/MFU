"""Opt-in SQL smoke test using only a temporary table."""

import os
import unittest


@unittest.skipUnless(os.environ.get("MFU_PROFILE_QA_SQL_TEST") == "1", "live DB test disabled")
class ProfileQaTimestampSqlTest(unittest.TestCase):
    def test_timestamp_changes_only_with_qa_text(self):
        from app.utils.db import get_db

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute("CREATE TEMPORARY TABLE profile_qa_timestamp_test (qa_text TEXT NULL, qa_updated_at DATETIME NULL)")
            cur.execute("INSERT INTO profile_qa_timestamp_test (qa_text) VALUES (%s)", ("Q1. 質問\n回答",))

            def save(value):
                cur.execute(
                    """UPDATE profile_qa_timestamp_test
                       SET qa_updated_at=CASE
                           WHEN NOT (qa_text <=> %s) THEN UTC_TIMESTAMP()
                           ELSE qa_updated_at END,
                           qa_text=%s""",
                    (value, value),
                )
                cur.execute("SELECT qa_updated_at FROM profile_qa_timestamp_test")
                return cur.fetchone()[0]

            self.assertIsNone(save("Q1. 質問\n回答"))
            changed_at = save("Q1. 質問\n新しい回答")
            self.assertIsNotNone(changed_at)
            self.assertEqual(save("Q1. 質問\n新しい回答"), changed_at)
        finally:
            cur.execute("DROP TEMPORARY TABLE IF EXISTS profile_qa_timestamp_test")
            db.rollback()
            cur.close()
            db.close()
