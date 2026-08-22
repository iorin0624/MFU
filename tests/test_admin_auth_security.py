from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AdminAuthenticationSecurityRegressionTests(unittest.TestCase):
    def text(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_web_login_has_no_network_based_mfa_bypass(self):
        source = self.text("__init__.py")
        login_block = source[source.index('@app.route("/login"'):source.index('@app.post("/logout"')]
        self.assertNotIn("ip_network", login_block)
        self.assertNotIn("is_local", login_block)
        self.assertIn("begin_password_preauth(username)", login_block)

    def test_chat_admin_requires_totp_and_has_no_ip_bypass(self):
        source = self.text("chat/gui_api.py")
        self.assertNotIn("ip_network", source)
        self.assertIn('username == ADMIN_USERNAME', source)
        self.assertIn('get_user_otp_secret(username)', source)

    def test_admin_passkey_is_password_second_factor(self):
        source = self.text("routes/webauthn_routes.py")
        self.assertIn('username = (session.get("user") or "").strip()', source)
        self.assertIn('password_preauth_valid(username)', source)
        self.assertIn('establish_admin_session(method="passkey"', source)

    def test_qr_only_login_requires_phone_passkey(self):
        qr_source = self.text("routes/admin_qr_auth.py")
        webauthn_source = self.text("routes/webauthn_routes.py")
        login_js = self.text("static/js/login.js")
        approval_js = self.text("static/js/admin-qr-approve.js")
        self.assertNotIn("if not password_preauth_valid():", qr_source)
        self.assertIn('method="qr_passkey"', qr_source)
        self.assertIn('"admin_qr_passkey_verified_token_hash"', qr_source)
        self.assertIn("def _qr_creation_rate_limited()", qr_source)
        self.assertIn("FROM admin_qr_login_challenges", qr_source)
        self.assertNotIn('record_attempt(ADMIN_USERNAME, "qr_create", False)', qr_source)
        self.assertIn('QR_APPROVAL_PURPOSE = "admin_qr_approval"', webauthn_source)
        self.assertIn('validate_admin_session(touch=False)', webauthn_source)
        self.assertIn('if (qrLoginEnabled) createQr();', login_js)
        self.assertIn('if (button.dataset.decision === "approve") await verifyPasskey();', approval_js)

    def test_qr_is_hidden_after_password_preauth(self):
        template = self.text("templates/login.html")
        self.assertIn("show_qr_login = not preauth_active and not approval_return", template)
        self.assertIn("{% if show_qr_login %}", template)
        self.assertIn("'true' if show_qr_login else 'false'", template)

    def test_restart_link_clears_password_preauth(self):
        source = self.text("__init__.py")
        template = self.text("templates/login.html")
        self.assertIn('if request.args.get("reset") == "1":', source)
        self.assertIn("clear_preauth()", source)
        self.assertIn("url_for('login', reset=1)", template)

    def test_qr_secret_is_not_an_http_path_parameter(self):
        source = self.text("routes/admin_qr_auth.py")
        self.assertNotIn('/approve/<token>', source)
        self.assertIn(' + "#" + token', source)
        self.assertIn("status='consumed'", source)

    def test_mutating_auth_routes_are_csrf_protected(self):
        source = self.text("__init__.py")
        for value in ('"/login"', '"/logout"', '"/auth/"', '"/mfa/"', '"/webauthn/"', '"/otp/"'):
            self.assertIn(value, source)
        self.assertIn('@app.post("/logout")', source)

    def test_direct_gunicorn_listener_is_restricted(self):
        source = self.text("deploy/mfu-app-firewall.sh")
        self.assertIn("192.168.103.15/32", source)
        self.assertIn("--dport 8080", source)
        self.assertIn("-j REJECT", source)


if __name__ == "__main__":
    unittest.main()
