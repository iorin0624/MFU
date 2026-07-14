"""Opt-in Square Sandbox smoke test; never uses production credentials or DB."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import time
import uuid

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("square_gateway_smoke", ROOT / "payment" / "square_gateway.py")
gateway = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = gateway
spec.loader.exec_module(gateway)


def require_sandbox_value(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing")
    return value


def main() -> int:
    if os.environ.get("RUN_SQUARE_SANDBOX_SMOKE") != "1":
        print("SKIP: set RUN_SQUARE_SANDBOX_SMOKE=1 to run")
        return 0

    env_file = Path(os.environ.get("MFU_ENV_FILE") or ROOT / ".env")
    load_dotenv(env_file)
    token = require_sandbox_value("SQUARE_SANDBOX_ACCESS_TOKEN")
    location_id = require_sandbox_value("SQUARE_SANDBOX_LOCATION_ID")
    base = "https://connect.squareupsandbox.com"
    payment_key = str(uuid.uuid4())
    payment_body = {
        "idempotency_key": payment_key,
        "source_id": "cnon:card-nonce-ok",
        "amount_money": {"amount": 1, "currency": "JPY"},
        "location_id": location_id,
        "reference_id": f"mfu:sandbox-smoke:{payment_key[:8]}",
        "customer_details": {"customer_initiated": True, "seller_keyed_in": False},
    }
    response = gateway.request_square(
        "POST",
        f"{base}/v2/payments",
        access_token=token,
        json_body=payment_body,
        idempotency_key=payment_key,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Sandbox CreatePayment failed: {gateway.square_error_info(response).code}")
    payment = (response.json() or {}).get("payment") or {}
    if payment.get("status") != "COMPLETED" or not payment.get("id"):
        raise RuntimeError(f"Unexpected Sandbox payment status: {payment.get('status')}")

    refund_key = str(uuid.uuid4())
    refund_body = {
        "idempotency_key": refund_key,
        "payment_id": payment["id"],
        "amount_money": {"amount": 1, "currency": "JPY"},
        "reason": "MFU Square safety smoke test",
    }
    response = gateway.request_square(
        "POST",
        f"{base}/v2/refunds",
        access_token=token,
        json_body=refund_body,
        idempotency_key=refund_key,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Sandbox RefundPayment failed: {gateway.square_error_info(response).code}")
    refund = (response.json() or {}).get("refund") or {}
    refund_id = refund.get("id")
    status = refund.get("status")
    for _ in range(5):
        if status in {"COMPLETED", "FAILED", "REJECTED"}:
            break
        time.sleep(1)
        response = gateway.request_square("GET", f"{base}/v2/refunds/{refund_id}", access_token=token)
        if response.status_code >= 400:
            raise RuntimeError(f"Sandbox GetPaymentRefund failed: {gateway.square_error_info(response).code}")
        status = ((response.json() or {}).get("refund") or {}).get("status")
    if status != "COMPLETED":
        raise RuntimeError(f"Unexpected Sandbox refund status: {status}")
    print(f"OK: Sandbox payment/refund completed with Square-Version {response.headers.get('Square-Version')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
