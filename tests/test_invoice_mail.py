import importlib.util
import sys
import types
import unittest
from email import message_from_string
from pathlib import Path
from unittest.mock import patch


def load_invoice_mail_module():
    repo_root = Path(__file__).resolve().parents[1]

    app_module = types.ModuleType("app")
    app_module.__path__ = []  # type: ignore[attr-defined]
    app_utils_module = types.ModuleType("app.utils")
    app_utils_module.__path__ = []  # type: ignore[attr-defined]
    app_utils_mail_module = types.ModuleType("app.utils.mail")
    app_utils_mail_module.send_mime = lambda msg: None
    sys.modules["app"] = app_module
    sys.modules["app.utils"] = app_utils_module
    sys.modules["app.utils.mail"] = app_utils_mail_module

    invoice_package = types.ModuleType("invoice")
    invoice_package.__path__ = [str(repo_root / "invoice")]  # type: ignore[attr-defined]
    sys.modules["invoice"] = invoice_package

    utils_spec = importlib.util.spec_from_file_location("invoice.utils", repo_root / "invoice" / "utils.py")
    utils_module = importlib.util.module_from_spec(utils_spec)
    assert utils_spec and utils_spec.loader
    sys.modules["invoice.utils"] = utils_module
    utils_spec.loader.exec_module(utils_module)

    services_module = types.ModuleType("invoice.services")
    services_module.log_mail_result = lambda *args, **kwargs: None
    services_module.mark_invoice_mailed = lambda invoice_id: None
    sys.modules["invoice.services"] = services_module

    mail_spec = importlib.util.spec_from_file_location("invoice.mail", repo_root / "invoice" / "mail.py")
    mail_module = importlib.util.module_from_spec(mail_spec)
    assert mail_spec and mail_spec.loader
    sys.modules["invoice.mail"] = mail_module
    mail_spec.loader.exec_module(mail_module)
    return mail_module


invoice_mail = load_invoice_mail_module()


class InvoiceMailTest(unittest.TestCase):
    def test_send_invoice_mail_uses_explicit_reply_to_and_cc(self):
        captured = {}

        def fake_send_mime(msg):
            captured["msg"] = msg

        with (
            patch.object(invoice_mail, "send_mime", side_effect=fake_send_mime),
            patch.object(invoice_mail, "mark_invoice_mailed"),
            patch.object(invoice_mail, "log_mail_result"),
        ):
            invoice_mail.send_invoice_mail(
                {
                    "id": 1,
                    "issuer_name": "いおりん写真室",
                    "issuer_email": "ignored@example.com",
                    "contact_email_snapshot": "contact@example.com",
                },
                to_email="to@example.com",
                cc_email="issuer@example.com, cc@example.com",
                bcc_email=None,
                reply_to_email="issuer@example.com",
                subject="件名",
                body="本文",
                attachment_filename="invoice.pdf",
                pdf_bytes=b"%PDF-1.4",
            )

        raw = captured["msg"].as_string()
        parsed = message_from_string(raw)
        self.assertIn("noreply@mail.iori0624.jp", parsed["From"])
        self.assertEqual(parsed["Reply-To"], "issuer@example.com")
        self.assertEqual(parsed["Cc"], "issuer@example.com, cc@example.com")

    def test_send_invoice_mail_skips_reply_to_when_explicit_value_is_blank(self):
        captured = {}

        def fake_send_mime(msg):
            captured["msg"] = msg

        with (
            patch.object(invoice_mail, "send_mime", side_effect=fake_send_mime),
            patch.object(invoice_mail, "mark_invoice_mailed"),
            patch.object(invoice_mail, "log_mail_result"),
        ):
            invoice_mail.send_invoice_mail(
                {
                    "id": 2,
                    "issuer_name": "いおりん写真室",
                    "issuer_email": "ignored@example.com",
                },
                to_email="to@example.com",
                cc_email=None,
                bcc_email=None,
                reply_to_email=None,
                subject="件名",
                body="本文",
                attachment_filename="invoice.pdf",
                pdf_bytes=b"%PDF-1.4",
            )

        raw = captured["msg"].as_string()
        parsed = message_from_string(raw)
        self.assertIsNone(parsed["Reply-To"])


if __name__ == "__main__":
    unittest.main()
