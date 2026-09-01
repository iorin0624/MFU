import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load_functions(path, names, ns):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8-sig"))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), path, "exec"), ns)


class PrivacyAgreementRedirectTests(unittest.TestCase):
    def namespace(self, path, headers=None, method="GET"):
        cursor = SimpleNamespace(execute=lambda *_: None, fetchone=lambda: {"id": 1}, close=lambda: None)
        db = SimpleNamespace(cursor=lambda **_: cursor, close=lambda: None)
        ns = {
            "session": {"ext_user_id": 1, "ext_after_privacy_policy_next": "/external-login/vue-preview/events/test"},
            "request": SimpleNamespace(path=path, full_path=path+"?", url="https://example.test"+path, blueprint="external_login_user", endpoint="test", method=method, headers=headers or {}, is_json=False),
            "_ALLOW_PRIVACY_POLICY_ENDPOINTS": set(), "_is_email_unverified": lambda: False,
            "get_db": lambda: db, "_get_current_privacy_policy_config": lambda: {},
            "_needs_privacy_policy_agreement": lambda *_: True,
            "jsonify": lambda value: value, "redirect": lambda value: ("redirect", value),
            "url_for": lambda _: "/external-login/",
            "current_app": SimpleNamespace(logger=SimpleNamespace(exception=lambda *a: None)),
        }
        load_functions("external_login_user/utils.py", {"_is_disallowed_ext_redirect_path", "_sanitize_ext_local_url"}, ns)
        load_functions("external_login_user/__init__.py", {"_is_vue_user_api_request", "_is_ext_background_request", "_lock_privacy_policy_globally"}, ns)
        load_functions("external_login_user/users.py", {"_resolve_privacy_policy_post_agree_next"}, ns)
        return ns

    def test_background_requests_never_replace_the_destination(self):
        for path, headers in [
            ("/external-login/updates/check", {}),
            ("/external-login/updates/text", {}),
            ("/external-login/api/vue/events", {}),
            ("/external-login/api/notifications", {}),
            ("/external-login/future-background", {"Sec-Fetch-Dest": "empty"}),
            ("/external-login/future-background", {"X-Requested-With": "XMLHttpRequest"}),
            ("/external-login/future-background", {"Accept": "application/json"}),
        ]:
            with self.subTest(path=path, headers=headers):
                ns = self.namespace(path, headers)
                result = ns["_lock_privacy_policy_globally"]()
                self.assertEqual(result, ({"ok": False, "error": "privacy_agreement_required"}, 403))
                self.assertEqual(ns["session"]["ext_after_privacy_policy_next"], "/external-login/vue-preview/events/test")

    def test_real_page_navigation_preserves_the_requested_page(self):
        ns = self.namespace("/external-login/events/view/test", {"Sec-Fetch-Dest": "document", "Accept": "text/html"})
        self.assertEqual(ns["_lock_privacy_policy_globally"](), ("redirect", "/external-login/"))
        self.assertEqual(ns["_resolve_privacy_policy_post_agree_next"](), "/external-login/events/view/test?")

    def test_previously_saved_json_and_text_destinations_are_discarded(self):
        for path in ("/external-login/updates/check?", "/external-login/updates/text", "/external-login/updates/ack", "/external-login/api/vue/events", "/chat/api/push/bootstrap"):
            with self.subTest(path=path):
                ns = self.namespace("/external-login/privacy-policy/agree")
                ns["session"]["ext_after_privacy_policy_next"] = path
                self.assertEqual(ns["_resolve_privacy_policy_post_agree_next"](), "/external-login/")
                self.assertNotIn("ext_after_privacy_policy_next", ns["session"])

    def test_post_request_is_not_a_browser_return_destination(self):
        ns = self.namespace("/external-login/events/test/my-role", {"Accept": "text/html"}, "POST")
        self.assertEqual(ns["_lock_privacy_policy_globally"](), ("redirect", "/external-login/"))
        self.assertEqual(ns["session"]["ext_after_privacy_policy_next"], "/external-login/vue-preview/events/test")

    def test_consent_form_saves_then_redirects_to_a_page_not_json(self):
        for destination, expected in [("/external-login/updates/check?", "/external-login/"), ("/external-login/vue-preview/events/test", "/external-login/vue-preview/events/test")]:
            with self.subTest(destination=destination):
                ns = self.namespace("/external-login/privacy-policy/agree", method="POST")
                ns["session"].update(ext_csrf="token", ext_after_privacy_policy_next=destination)
                ns["request"].form = {"csrf_token": "token"}
                saved = []
                ns.update(_require_ext_login=lambda: None, flash=lambda *_: None,
                          _is_privacy_policy_effective=lambda _: True,
                          _agree_current_privacy_policy=lambda uid, source: saved.append((uid, source)) or True,
                          _resolve_ext_login_prerequisite_redirect=lambda *a, **kw: None)
                load_functions("external_login_user/users.py", {"privacy_policy_agree"}, ns)
                self.assertEqual(ns["privacy_policy_agree"](), ("redirect", expected))
                self.assertEqual(saved, [(1, "top")])


if __name__ == "__main__":
    unittest.main()
