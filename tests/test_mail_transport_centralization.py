import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIL_MODULE = ROOT / "utils" / "mail.py"
FORBIDDEN_TRANSPORT_KEYWORDS = {"smtp_host", "smtp_port", "starttls"}


def _runtime_python_files():
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if "tests" in relative.parts or "__pycache__" in relative.parts:
            continue
        if ".bak" in path.name:
            continue
        yield path


class MailTransportCentralizationTest(unittest.TestCase):
    def test_only_utils_mail_opens_smtp_connections(self):
        violations = []
        for path in _runtime_python_files():
            if path == MAIL_MODULE:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "smtplib"
                    and node.func.attr in {"SMTP", "SMTP_SSL"}
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(violations, [])

    def test_callers_cannot_override_the_shared_smtp_transport(self):
        violations = []
        for path in _runtime_python_files():
            if path == MAIL_MODULE:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function_name = ""
                if isinstance(node.func, ast.Name):
                    function_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    function_name = node.func.attr
                if function_name not in {"send_mail", "send_mime"}:
                    continue
                forbidden = sorted(
                    keyword.arg
                    for keyword in node.keywords
                    if keyword.arg in FORBIDDEN_TRANSPORT_KEYWORDS
                )
                if forbidden:
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno}:{','.join(forbidden)}"
                    )
        self.assertEqual(violations, [])

    def test_shared_mail_transport_requires_authenticated_starttls_on_587(self):
        source = MAIL_MODULE.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(MAIL_MODULE))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name in ("send_mail", "send_mime"):
            function = functions[function_name]
            arguments = {argument.arg for argument in function.args.args + function.args.kwonlyargs}
            self.assertTrue(FORBIDDEN_TRANSPORT_KEYWORDS.isdisjoint(arguments))
            function_source = ast.get_source_segment(source, function) or ""
            self.assertIn("_load_smtp_settings()", function_source)
            self.assertIn("smtp.starttls", function_source)
            self.assertIn("smtp.login", function_source)

        loader_source = ast.get_source_segment(source, functions["_load_smtp_settings"]) or ""
        self.assertIn("port != 587", loader_source)
        self.assertIn("not starttls", loader_source)


if __name__ == "__main__":
    unittest.main()
