from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from typing import Any


MAX_BODY_BYTES = 128_000
SIGNATURE_WINDOW_SECONDS = 300


class DiagnosticValidationError(ValueError):
    pass


def verify_signed_body(body: bytes, timestamp: str, signature: str, secret: str, *, now: int | None = None) -> None:
    if not secret:
        raise DiagnosticValidationError("診断APIの共有鍵が設定されていません")
    if not timestamp or not re.fullmatch(r"\d{10,12}", timestamp):
        raise DiagnosticValidationError("署名時刻が不正です")
    current = int(time.time()) if now is None else int(now)
    if abs(current - int(timestamp)) > SIGNATURE_WINDOW_SECONDS:
        raise DiagnosticValidationError("署名の有効時間を超えています")
    expected = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    supplied = (signature or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", supplied) or not hmac.compare_digest(expected, supplied):
        raise DiagnosticValidationError("署名が一致しません")


def parse_semicolon_metrics(value: object) -> dict[str, float | int | str]:
    result: dict[str, float | int | str] = {}
    for part in str(value or "").split(";"):
        if "=" not in part:
            continue
        key, raw = part.split("=", 1)
        key = key.strip().lower()
        raw = raw.strip()
        if not key or len(key) > 64 or len(raw) > 128:
            continue
        try:
            if re.fullmatch(r"-?\d+", raw):
                result[key] = int(raw)
            elif re.fullmatch(r"-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", raw, re.I):
                result[key] = float(raw)
            else:
                result[key] = raw
        except (TypeError, ValueError):
            result[key] = raw
    return result


def _number(metrics: dict[str, Any], key: str) -> float | None:
    try:
        value = metrics.get(key)
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _count(metrics: dict[str, Any], key: str) -> int | None:
    value = _number(metrics, key)
    return None if value is None else max(0, int(value))


def _ratio_percent(lost: int | None, total: int | None) -> float | None:
    if lost is None or total is None or total <= 0:
        return None
    return round(max(0.0, lost) * 100.0 / total, 4)


def _seconds_to_ms(value: float | None) -> float | None:
    if value is None or value < 0:
        return None
    return round(value * 1000.0, 3)


def normalize_call_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if str(payload.get("endpoint") or "") != "10610":
        raise DiagnosticValidationError("対象外の内線です")
    event_id = str(payload.get("event_id") or payload.get("uniqueid") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", event_id):
        raise DiagnosticValidationError("event_idが不正です")

    qos = parse_semicolon_metrics(payload.get("qos_all"))
    jitter = parse_semicolon_metrics(payload.get("qos_jitter"))
    loss = parse_semicolon_metrics(payload.get("qos_loss"))
    rtt_metrics = parse_semicolon_metrics(payload.get("qos_rtt"))
    mes = parse_semicolon_metrics(payload.get("qos_mes"))

    local_packets = _count(qos, "rxcount")
    remote_packets = _count(qos, "txcount")
    local_lost = _count(qos, "lp")
    remote_lost = _count(qos, "rlp")
    local_loss_pct = _ratio_percent(local_lost, local_packets)
    remote_loss_pct = _ratio_percent(remote_lost, remote_packets)

    rx_jitter_ms = _seconds_to_ms(_number(qos, "rxjitter"))
    tx_jitter_ms = _seconds_to_ms(_number(qos, "txjitter"))
    remote_avg_jitter_ms = _seconds_to_ms(_number(jitter, "reported_avgjitter"))
    rtt_ms = _seconds_to_ms(_number(qos, "rtt"))
    if rtt_ms is None:
        rtt_ms = _seconds_to_ms(_number(rtt_metrics, "avgrtt"))

    quality_status = quality_grade(
        remote_loss_pct=remote_loss_pct,
        remote_jitter_ms=remote_avg_jitter_ms if remote_avg_jitter_ms is not None else tx_jitter_ms,
        rtt_ms=rtt_ms,
    )

    started_at = _parse_datetime(payload.get("started_at"))
    ended_at = _parse_datetime(payload.get("ended_at")) or datetime.now()
    return {
        "event_id": event_id,
        "uniqueid": str(payload.get("uniqueid") or "")[:96],
        "linkedid": str(payload.get("linkedid") or "")[:96],
        "started_at": started_at,
        "ended_at": ended_at,
        "duration": _safe_int(payload.get("duration")),
        "billsec": _safe_int(payload.get("billsec")),
        "direction": str(payload.get("direction") or "outbound")[:16],
        "endpoint": "10610",
        "remote_number": re.sub(r"[^0-9*#+]", "", str(payload.get("remote_number") or ""))[:32],
        "channel_name": str(payload.get("channel_name") or "")[:160],
        "read_codec": str(payload.get("read_codec") or "")[:64],
        "write_codec": str(payload.get("write_codec") or "")[:64],
        "rtp_source": str(payload.get("rtp_source") or "")[:128],
        "rtp_dest": str(payload.get("rtp_dest") or "")[:128],
        "sip_remote_addr": str(payload.get("sip_remote_addr") or "")[:128],
        "sip_call_id": str(payload.get("sip_call_id") or "")[:255],
        "local_packets": local_packets,
        "remote_packets": remote_packets,
        "local_lost": local_lost,
        "remote_lost": remote_lost,
        "local_loss_pct": local_loss_pct,
        "remote_loss_pct": remote_loss_pct,
        "rx_jitter_ms": rx_jitter_ms,
        "tx_jitter_ms": tx_jitter_ms,
        "remote_avg_jitter_ms": remote_avg_jitter_ms,
        "rtt_ms": rtt_ms,
        "tx_mes": _number(qos, "txmes"),
        "rx_mes": _number(qos, "rxmes"),
        "quality_status": quality_status,
        "raw_metrics": json.dumps(
            {"qos": qos, "jitter": jitter, "loss": loss, "rtt": rtt_metrics, "mes": mes},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def quality_grade(*, remote_loss_pct: float | None, remote_jitter_ms: float | None, rtt_ms: float | None) -> str:
    available = [value for value in (remote_loss_pct, remote_jitter_ms, rtt_ms) if value is not None]
    if not available:
        return "unknown"
    if ((remote_loss_pct is not None and remote_loss_pct >= 3.0)
            or (remote_jitter_ms is not None and remote_jitter_ms >= 30.0)
            or (rtt_ms is not None and rtt_ms >= 300.0)):
        return "bad"
    if ((remote_loss_pct is not None and remote_loss_pct >= 1.0)
            or (remote_jitter_ms is not None and remote_jitter_ms >= 20.0)
            or (rtt_ms is not None and rtt_ms >= 200.0)):
        return "warning"
    return "good"


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
        except ValueError:
            continue
    return None


def _safe_int(value: object) -> int:
    try:
        return max(0, int(float(str(value or "0"))))
    except (TypeError, ValueError):
        return 0
