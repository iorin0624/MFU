import unittest
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "payment" / "bulk_refund_logic.py"
spec = importlib.util.spec_from_file_location("bulk_refund_logic", MODULE_PATH)
bulk_refund_logic = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(bulk_refund_logic)

decide_bulk_refund_status = bulk_refund_logic.decide_bulk_refund_status
build_preview_hash = bulk_refund_logic.build_preview_hash
build_refund_note = bulk_refund_logic.build_refund_note
append_note_if_missing = bulk_refund_logic.append_note_if_missing
recalculate_paid_amount = bulk_refund_logic.recalculate_paid_amount


class BulkRefundLogicTest(unittest.TestCase):
    def test_diff_non_positive(self):
        status, reason = decide_bulk_refund_status(
            has_member=True, member_event_match=True, square_status="COMPLETED", override_fee=0, diff=0, remaining=100
        )
        self.assertEqual((status, reason), ("excluded", "diff_non_positive"))

    def test_fee_override_manual(self):
        status, reason = decide_bulk_refund_status(
            has_member=True, member_event_match=True, square_status="COMPLETED", override_fee=100, diff=200, remaining=200
        )
        self.assertEqual((status, reason), ("manual", "member_fee_override_present"))

    def test_already_refunded(self):
        status, reason = decide_bulk_refund_status(
            has_member=True, member_event_match=True, square_status="COMPLETED", override_fee=0, diff=300, remaining=0
        )
        self.assertEqual((status, reason), ("excluded", "already_refunded"))

    def test_eligible(self):
        status, reason = decide_bulk_refund_status(
            has_member=True, member_event_match=True, square_status="COMPLETED", override_fee=0, diff=200, remaining=500
        )
        self.assertEqual((status, reason), ("eligible", "eligible"))

    def test_approved_payment_is_not_refundable_until_completed(self):
        status, reason = decide_bulk_refund_status(
            has_member=True, member_event_match=True, square_status="APPROVED", override_fee=0, diff=200, remaining=500
        )
        self.assertEqual((status, reason), ("excluded", "non_success_status"))

    def test_preview_hash_stable(self):
        rows = [
            {"payment_row_id": 2, "paid": 4490, "current_fee": 4200, "refunded_sum": 0, "refunded_diff_total": 0, "remaining_refundable": 290, "diff": 290, "remaining_diff": 290, "status": "eligible", "reason_code": "eligible"},
            {"payment_row_id": 1, "paid": 4200, "current_fee": 4200, "refunded_sum": 0, "refunded_diff_total": 0, "remaining_refundable": 0, "diff": 0, "remaining_diff": 0, "status": "excluded", "reason_code": "diff_non_positive"},
        ]
        h1 = build_preview_hash(secret="test", payment_event_id=5, payment_event_uuid="abc", external_event_id=4, rows=rows)
        h2 = build_preview_hash(secret="test", payment_event_id=5, payment_event_uuid="abc", external_event_id=4, rows=list(reversed(rows)))
        self.assertEqual(h1, h2)

    def test_preview_hash_changes(self):
        rows = [{"payment_row_id": 2, "paid": 4490, "current_fee": 4200, "refunded_sum": 0, "refunded_diff_total": 0, "remaining_refundable": 290, "diff": 290, "remaining_diff": 290, "status": "eligible", "reason_code": "eligible"}]
        h1 = build_preview_hash(secret="test", payment_event_id=5, payment_event_uuid="abc", external_event_id=4, rows=rows)
        rows2 = [{"payment_row_id": 2, "paid": 4490, "current_fee": 4200, "refunded_sum": 100, "refunded_diff_total": 100, "remaining_refundable": 190, "diff": 290, "remaining_diff": 190, "status": "eligible", "reason_code": "eligible"}]
        h2 = build_preview_hash(secret="test", payment_event_id=5, payment_event_uuid="abc", external_event_id=4, rows=rows2)
        self.assertNotEqual(h1, h2)

    def test_recalculate_paid_amount(self):
        self.assertEqual(recalculate_paid_amount(original_paid=4490, refunded_total=290), 4200)

    def test_refund_note_and_dedup(self):
        note = build_refund_note(dt=__import__('datetime').datetime(2026, 2, 11), refund_yen=290)
        self.assertEqual(note, "2026年2月11日に290円差額返金済")
        once = append_note_if_missing("既存メモ", note)
        self.assertIn(note, once)
        twice = append_note_if_missing(once, note)
        self.assertEqual(twice, once)


class ReceiptTemplateGuardTest(unittest.TestCase):
    def test_receipt_template_uses_receipt_note_not_admin_note(self):
        tpl = (Path(__file__).resolve().parents[1] / "external_login_user" / "template" / "receipt_pdf.html").read_text(encoding="utf-8")
        self.assertIn("receipt.receipt_note", tpl)
        self.assertNotIn("admin_note", tpl)


    def test_admin_member_edit_has_receipt_note_input(self):
        tpl = (Path(__file__).resolve().parents[1] / "external_login_user" / "template" / "admin_member_edit.html").read_text(encoding="utf-8")
        self.assertIn('name="receipt_note"', tpl)

    def test_receipt_template_uses_wareki_like_labels(self):
        tpl = (Path(__file__).resolve().parents[1] / "external_login_user" / "template" / "receipt_pdf.html").read_text(encoding="utf-8")
        self.assertIn("issue_date_label", tpl)
        self.assertIn("pay_date_label", tpl)
        self.assertNotIn("%Y-%m-%d", tpl)


if __name__ == "__main__":
    unittest.main()
