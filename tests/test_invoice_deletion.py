import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_services():
    app_module = sys.modules.setdefault("app", types.ModuleType("app"))
    app_module.__path__ = []  # type: ignore[attr-defined]
    app_utils_module = sys.modules.setdefault("app.utils", types.ModuleType("app.utils"))
    app_utils_module.__path__ = []  # type: ignore[attr-defined]
    app_utils_db_module = types.ModuleType("app.utils.db")
    app_utils_db_module.get_db = lambda: None
    sys.modules["app.utils.db"] = app_utils_db_module

    package = types.ModuleType("invoice_delete_test")
    package.__path__ = [str(ROOT / "invoice")]  # type: ignore[attr-defined]
    sys.modules["invoice_delete_test"] = package

    utils_spec = importlib.util.spec_from_file_location(
        "invoice_delete_test.utils", ROOT / "invoice" / "utils.py"
    )
    utils_module = importlib.util.module_from_spec(utils_spec)
    assert utils_spec and utils_spec.loader
    sys.modules["invoice_delete_test.utils"] = utils_module
    utils_spec.loader.exec_module(utils_module)

    services_spec = importlib.util.spec_from_file_location(
        "invoice_delete_test.services", ROOT / "invoice" / "services.py"
    )
    services_module = importlib.util.module_from_spec(services_spec)
    assert services_spec and services_spec.loader
    sys.modules["invoice_delete_test.services"] = services_module
    services_spec.loader.exec_module(services_module)
    return services_module


services = load_services()


class FakeCursor:
    def __init__(self, invoice, payment=None):
        self.invoice = invoice
        self.payment = payment
        self.current_result = None
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        self.rowcount = 0
        if "FROM invoice_headers" in normalized and "FOR UPDATE" in normalized:
            self.current_result = dict(self.invoice) if self.invoice else None
        elif "FROM invoice_card_payments" in normalized and "LIMIT 1" in normalized:
            payment_status = str((self.payment or {}).get("square_status") or "").upper()
            compared_statuses = {str(value).upper() for value in (params or ())[1:]}
            matches = payment_status in compared_statuses
            if "NOT IN" in normalized:
                matches = not matches
            self.current_result = dict(self.payment) if self.payment and matches else None
        else:
            self.current_result = None
            if normalized.startswith("UPDATE ") or normalized.startswith("DELETE FROM invoice_headers"):
                self.rowcount = 1

    def fetchone(self):
        return self.current_result

    def close(self):
        pass


class FakeDb:
    def __init__(self, invoice, payment=None):
        self.fake_cursor = FakeCursor(invoice, payment)
        self.committed = False
        self.rolled_back = False

    def cursor(self, dictionary=False):
        return self.fake_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class InvoiceDeletionTest(unittest.TestCase):
    def test_issued_invoice_cannot_be_soft_deleted(self):
        db = FakeDb({"id": 1, "invoice_no": "INV-1", "status": "issued", "deleted_at": None})
        with patch.object(services, "get_db", return_value=db):
            with self.assertRaises(services.InvoiceValidationError):
                services.soft_delete_invoice(1, deleted_by="admin")
        self.assertFalse(db.committed)
        self.assertTrue(db.rolled_back)
        self.assertFalse(any(sql.startswith("UPDATE invoice_headers") for sql, _ in db.fake_cursor.executed))

    def test_draft_with_pending_square_payment_cannot_be_deleted(self):
        db = FakeDb(
            {"id": 2, "invoice_no": "INV-2", "status": "draft", "deleted_at": None},
            {"square_status": "PENDING"},
        )
        with patch.object(services, "get_db", return_value=db):
            with self.assertRaises(services.InvoiceValidationError):
                services.soft_delete_invoice(2, deleted_by="admin")
        self.assertFalse(db.committed)
        self.assertTrue(db.rolled_back)

    def test_cancelled_invoice_with_completed_payment_can_be_soft_deleted(self):
        db = FakeDb(
            {"id": 5, "invoice_no": "INV-5", "status": "cancelled", "deleted_at": None},
            {"square_status": "COMPLETED"},
        )
        with patch.object(services, "get_db", return_value=db):
            result = services.soft_delete_invoice(5, deleted_by="admin")
        self.assertEqual(result["invoice_no"], "INV-5")
        self.assertTrue(db.committed)

    def test_draft_without_protected_payment_moves_to_deleted_list(self):
        db = FakeDb({"id": 3, "invoice_no": "INV-3", "status": "draft", "deleted_at": None})
        with patch.object(services, "get_db", return_value=db):
            result = services.soft_delete_invoice(3, deleted_by="admin")
        self.assertEqual(result["invoice_no"], "INV-3")
        self.assertTrue(db.committed)
        self.assertTrue(any(sql.startswith("UPDATE invoice_headers") for sql, _ in db.fake_cursor.executed))
        self.assertTrue(any("INSERT INTO invoice_deletion_audit" in sql for sql, _ in db.fake_cursor.executed))

    def test_purge_requires_matching_invoice_number(self):
        db = FakeDb(
            {"id": 4, "invoice_no": "INV-4", "status": "cancelled", "deleted_at": object()}
        )
        with patch.object(services, "get_db", return_value=db):
            with self.assertRaises(services.InvoiceValidationError):
                services.purge_deleted_invoice(4, confirmed_invoice_no="wrong", purged_by="admin")
        self.assertFalse(db.committed)
        self.assertTrue(db.rolled_back)

    def test_normal_and_public_queries_exclude_deleted_invoices(self):
        source = (ROOT / "invoice" / "services.py").read_text(encoding="utf-8")
        self.assertIn('where: list[str] = ["deleted_at IS NULL"]', source)
        self.assertIn("card_payment_public_token=%s AND deleted_at IS NULL", source)

    def test_delete_button_is_limited_to_deletable_statuses(self):
        source = (ROOT / "invoice" / "template" / "invoice_list.html").read_text(encoding="utf-8")
        self.assertIn("invoice.status in deletable_statuses", source)
        self.assertIn("csrf_token", source)

    def test_invoice_list_uses_cards_without_horizontal_table_scroll(self):
        source = (ROOT / "invoice" / "template" / "invoice_list.html").read_text(encoding="utf-8")
        self.assertIn('class="invoice-card-list"', source)
        self.assertIn('class="invoice-card"', source)
        self.assertNotIn('class="table-responsive"', source)
        self.assertNotIn("invoice-list-table", source)

    def test_index_check_drains_composite_index_rows(self):
        class IndexCursor:
            def __init__(self):
                self.fetchall_called = False

            def execute(self, sql, params):
                pass

            def fetchall(self):
                self.fetchall_called = True
                return [{"Seq_in_index": 1}, {"Seq_in_index": 2}]

        cursor = IndexCursor()
        self.assertTrue(services._index_exists(cursor, "invoice_headers", "idx_deleted"))
        self.assertTrue(cursor.fetchall_called)


if __name__ == "__main__":
    unittest.main()
