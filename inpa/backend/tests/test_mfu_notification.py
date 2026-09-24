import hashlib
import hmac

from flask import Flask

from inpa_app import mfu_notification


def test_feedback_notification_is_hmac_signed(monkeypatch):
    captured = {}

    class Response:
        status = 200

        @staticmethod
        def read():
            return b'{"delivered":true}'

    class Connection:
        def __init__(self, host, port, timeout):
            captured.update(host=host, port=port, timeout=timeout)

        def request(self, method, path, body, headers):
            captured.update(method=method, path=path, body=body, headers=headers)

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close():
            return None

    monkeypatch.setattr(mfu_notification.http.client, "HTTPConnection", Connection)
    app = Flask(__name__)
    app.config.update(
        INTERNAL_ADMIN_HMAC_SECRET="test-shared-secret",
        MFU_NOTIFICATION_HOST="127.0.0.1",
        MFU_NOTIFICATION_PORT=8080,
    )
    with app.app_context():
        mfu_notification.send_feedback_notification({"event_id": "01TEST", "message": "test"})

    headers = captured["headers"]
    digest = hashlib.sha256(captured["body"]).hexdigest()
    canonical = (
        f"POST\n{captured['path']}\n{headers['X-INPA-Timestamp']}\n"
        f"{headers['X-INPA-Nonce']}\n{digest}\ninpa-feedback"
    ).encode()
    expected = hmac.new(b"test-shared-secret", canonical, hashlib.sha256).hexdigest()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8080
    assert captured["path"] == "/api/internal/inpa/feedback-notification"
    assert hmac.compare_digest(headers["X-INPA-Signature"], expected)
