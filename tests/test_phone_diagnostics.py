import hashlib
import hmac
import json

import pytest

from app.phone_diagnostics.service import (
    DiagnosticValidationError,
    normalize_call_payload,
    parse_semicolon_metrics,
    verify_signed_body,
)


def test_verify_signed_body_accepts_valid_signature():
    body = b'{"event_id":"test"}'
    timestamp = "1784460000"
    secret = "s" * 32
    signature = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    verify_signed_body(body, timestamp, signature, secret, now=1784460000)


def test_verify_signed_body_rejects_expired_signature():
    with pytest.raises(DiagnosticValidationError):
        verify_signed_body(b"{}", "1784460000", "0" * 64, "s" * 32, now=1784461000)


def test_normalize_call_payload_calculates_downlink_quality():
    item = normalize_call_payload(
        {
            "event_id": "1784457842.10",
            "uniqueid": "1784457842.10",
            "endpoint": "10610",
            "ended_at": "2026-07-19 19:44:31",
            "qos_all": "rxcount=1000;txcount=1000;lp=1;rlp=42;rxjitter=0.004;txjitter=0.038;rtt=0.350;txmes=2.5;rxmes=4.1",
            "qos_jitter": "reported_avgjitter=0.041",
        }
    )
    assert item["local_loss_pct"] == 0.1
    assert item["remote_loss_pct"] == 4.2
    assert item["remote_avg_jitter_ms"] == 41.0
    assert item["rtt_ms"] == 350.0
    assert item["quality_status"] == "bad"
    assert json.loads(item["raw_metrics"])["qos"]["rlp"] == 42


def test_parse_semicolon_metrics_ignores_invalid_parts():
    assert parse_semicolon_metrics("rxcount=10;broken;value=0.25") == {"rxcount": 10, "value": 0.25}
