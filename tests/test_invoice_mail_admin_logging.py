import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def function_node(path: Path, name: str):
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)


def function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8-sig")
    node = function_node(path, name)
    return ast.get_source_segment(source, node) or ""


class InvoiceMailAccessLogVisibilityTest(unittest.TestCase):
    def test_invoice_mail_post_matcher_is_narrow(self):
        source = (ROOT / "__init__.py").read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_ADMINLOGS_INVOICE_MAIL_POST_RE" for target in node.targets)
        )
        matcher = function_node(ROOT / "__init__.py", "_adminlogs_is_invoice_mail_post")
        import re
        namespace = {"re": re}
        exec(compile(ast.Module(body=[assignment, matcher], type_ignores=[]), "__init__.py", "exec"), namespace)
        check = namespace["_adminlogs_is_invoice_mail_post"]

        self.assertTrue(check("POST /invoice/123/mail 302 UA=\"Safari\""))
        self.assertFalse(check("GET /invoice/123/mail 302 UA=\"Safari\""))
        self.assertFalse(check("POST /invoice/list 302 UA=\"Safari\""))
        self.assertFalse(check("POST /payment/admin/events 302 UA=\"Safari\""))

    def test_only_invoice_mail_redirect_bypasses_global_3xx_filter(self):
        source = function_source(ROOT / "__init__.py", "_build_admin_logs_html")
        self.assertIn("300 <= st < 400 and not _adminlogs_is_invoice_mail_post(text)", source)


class MimeSmtpGeneralLogTest(unittest.TestCase):
    def test_send_mime_writes_smtp_summary_with_full_recipient_list(self):
        source = function_source(ROOT / "utils" / "mail.py", "send_mime")
        self.assertIn("write_smtp_log(line)", source)
        self.assertIn('recipient_text = ", ".join(rcpts)', source)
        self.assertIn("_summary_log(True, recipient_text, subj)", source)
        self.assertNotIn("_mask_email_for_log", source)


if __name__ == "__main__":
    unittest.main()
