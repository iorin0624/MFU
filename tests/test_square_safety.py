import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

try:
    import requests
except ModuleNotFoundError:
    requests = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class Timeout(RequestException):
        pass

    requests.RequestException = RequestException
    requests.Timeout = Timeout
    requests.Response = object
    requests.request = lambda *args, **kwargs: None
    sys.modules["requests"] = requests


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gateway = load_module("square_gateway_under_test", "payment/square_gateway.py")
state = load_module("square_state_under_test", "payment/square_state.py")


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text
        self.content = b"{}"

    def json(self):
        return self._payload


class SquareGatewaySafetyTest(unittest.TestCase):
    def test_version_is_pinned_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SQUARE_API_VERSION", None)
            headers = gateway.square_headers("secret")
        self.assertEqual(headers["Square-Version"], "2025-08-20")
        self.assertEqual(headers["Authorization"], "Bearer secret")

    def test_post_without_idempotency_is_not_retried(self):
        calls = []

        def transport(*args, **kwargs):
            calls.append((args, kwargs))
            raise requests.Timeout("unknown result")

        with self.assertRaises(gateway.SquareTransportError):
            gateway.request_square(
                "POST",
                "https://example.test/v2/payments",
                access_token="token",
                json_body={"source_id": "nonce"},
                transport=transport,
                sleeper=lambda _: None,
            )
        self.assertEqual(len(calls), 1)

    def test_idempotent_post_retries_same_body(self):
        calls = []
        body = {"idempotency_key": "same-key", "source_id": "nonce"}

        def transport(*args, **kwargs):
            calls.append(kwargs["json"])
            if len(calls) == 1:
                raise requests.Timeout("unknown result")
            return FakeResponse(200, {"payment": {"status": "COMPLETED"}})

        response = gateway.request_square(
            "POST",
            "https://example.test/v2/payments",
            access_token="token",
            json_body=body,
            idempotency_key="same-key",
            transport=transport,
            sleeper=lambda _: None,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [body, body])

    def test_idempotency_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            gateway.request_square(
                "POST",
                "https://example.test/v2/refunds",
                access_token="token",
                json_body={"idempotency_key": "body-key"},
                idempotency_key="different-key",
            )


class SquareStateSafetyTest(unittest.TestCase):
    def test_only_completed_refunds_are_counted(self):
        rows = [
            {"status": "COMPLETED", "amount_yen": 100},
            {"status": "PENDING", "amount_yen": 200},
            {"status": "FAILED", "amount_yen": 300},
            {"status": "REJECTED", "amount_yen": 400},
        ]
        self.assertEqual(state.completed_refund_total(rows), 100)

    def test_pending_refund_is_not_completed(self):
        self.assertFalse(state.is_refund_completed("PENDING"))
        self.assertTrue(state.is_refund_completed("completed"))

    def test_older_webhook_is_rejected(self):
        self.assertFalse(
            state.should_apply_square_update(
                "2026-07-13T02:00:00Z",
                "2026-07-13T01:59:59Z",
            )
        )
        self.assertTrue(
            state.should_apply_square_update(
                "2026-07-13T02:00:00Z",
                "2026-07-13T02:00:01Z",
            )
        )


class SquareIntegrationGuardTest(unittest.TestCase):
    def test_refund_schema_can_store_completed(self):
        source = (ROOT / "payment" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("ENUM('PENDING','UNKNOWN','APPROVED','COMPLETED','REJECTED','FAILED','CANCELED')", source)

    def test_existing_rows_are_cut_off_from_reconciliation(self):
        source = (ROOT / "payment" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("square_sync_control", source)
        self.assertIn("created_at >= %s", source)
        self.assertIn("legacy_or_unknown_refund", source)

    def test_square_calls_use_shared_gateway(self):
        payment_source = (ROOT / "payment" / "__init__.py").read_text(encoding="utf-8")
        invoice_source = (ROOT / "invoice" / "routes.py").read_text(encoding="utf-8")
        self.assertNotIn('requests.post(\n            f"{_square_api_base()}/v2/payments"', payment_source)
        self.assertIn("request_square(", invoice_source)

    def test_invoice_otp_verify_uses_loaded_invoice(self):
        source = (ROOT / "invoice" / "routes.py").read_text(encoding="utf-8")
        verify_handler = source.split("def invoice_card_otp_verify", 1)[1].split(
            "def invoice_card_precheck", 1
        )[0]
        self.assertIn('_invoice.get("contact_name_snapshot")', verify_handler)
        self.assertNotIn('(invoice.get("contact_name_snapshot")', verify_handler)

    def test_invoice_thanks_is_public_standalone_page(self):
        source = (ROOT / "invoice" / "template" / "invoice_card_pay_thanks.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("<!DOCTYPE html>", source)
        self.assertNotIn('{% extends "base.html" %}', source)


if __name__ == "__main__":
    unittest.main()
