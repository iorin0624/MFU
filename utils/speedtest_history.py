"""Persistent, admin-only history for the MFU upload speed test."""

from __future__ import annotations

import ipaddress
import math
import statistics
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Request

from app.utils.db import get_db
from app.utils.speedtest import MEBIBYTE, SPEEDTEST_UPLOAD_SIZES_MB
from app.utils.whois_util import get_netinfo


SPEEDTEST_HISTORY_RETENTION_DAYS = 365
SPEEDTEST_PING_SAMPLE_COUNT = 10
SPEEDTEST_UPLOAD_ROUNDS = 3
SPEEDTEST_HISTORY_LIST_LIMIT = 50

_SCHEMA_LOCK = threading.Lock()
_schema_ready = False


class SpeedtestHistoryError(ValueError):
    """Raised when a completed speed-test result cannot be accepted."""


def ensure_speedtest_history_schema() -> None:
    """Create the history tables once per application worker."""
    global _schema_ready
    if _schema_ready:
        return
    with _SCHEMA_LOCK:
        if _schema_ready:
            return
        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS speedtest_results (
                  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  test_id CHAR(36) NOT NULL,
                  completed_at_utc DATETIME(6) NOT NULL,
                  ip_address VARCHAR(64) NOT NULL,
                  overall_mbps DECIMAL(14,3) NOT NULL,
                  ping_median_ms DECIMAL(14,3) NOT NULL,
                  jitter_ms DECIMAL(14,3) NOT NULL,
                  estimated_500mb_seconds DECIMAL(14,3) NOT NULL,
                  total_elapsed_ms DECIMAL(16,3) NOT NULL,
                  total_bytes BIGINT UNSIGNED NOT NULL,
                  retry_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
                  result_version SMALLINT UNSIGNED NOT NULL DEFAULT 1,
                  PRIMARY KEY (id),
                  UNIQUE KEY uq_speedtest_result_test_id (test_id),
                  KEY idx_speedtest_completed (completed_at_utc),
                  KEY idx_speedtest_ip_completed (ip_address, completed_at_utc)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS speedtest_ping_samples (
                  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  result_id BIGINT UNSIGNED NOT NULL,
                  sample_no SMALLINT UNSIGNED NOT NULL,
                  duration_ms DECIMAL(14,3) NOT NULL,
                  PRIMARY KEY (id),
                  UNIQUE KEY uq_speedtest_ping_sample (result_id, sample_no),
                  CONSTRAINT fk_speedtest_ping_result
                    FOREIGN KEY (result_id) REFERENCES speedtest_results (id)
                    ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS speedtest_upload_samples (
                  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                  result_id BIGINT UNSIGNED NOT NULL,
                  sample_no SMALLINT UNSIGNED NOT NULL,
                  round_no SMALLINT UNSIGNED NOT NULL,
                  size_mb SMALLINT UNSIGNED NOT NULL,
                  bytes_sent BIGINT UNSIGNED NOT NULL,
                  client_duration_ms DECIMAL(16,3) NOT NULL,
                  server_elapsed_ms DECIMAL(16,3) NULL,
                  mbps DECIMAL(14,3) NOT NULL,
                  retried TINYINT(1) NOT NULL DEFAULT 0,
                  PRIMARY KEY (id),
                  UNIQUE KEY uq_speedtest_upload_sample (result_id, sample_no),
                  KEY idx_speedtest_upload_result_size (result_id, size_mb, round_no),
                  CONSTRAINT fk_speedtest_upload_result
                    FOREIGN KEY (result_id) REFERENCES speedtest_results (id)
                    ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            db.commit()
        finally:
            db.close()
        _schema_ready = True


def request_ip(flask_request: Request) -> str:
    """Return the address normalized by the configured one-hop ProxyFix."""
    value = str(flask_request.remote_addr or "").strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value[:64] or "-"


def _number(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise SpeedtestHistoryError(f"{name}が不正です。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SpeedtestHistoryError(f"{name}が不正です。") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise SpeedtestHistoryError(f"{name}が許容範囲外です。")
    return number


def normalize_speedtest_result(payload: Any) -> dict[str, Any]:
    """Validate raw browser timings and calculate every derived value server-side."""
    if not isinstance(payload, dict):
        raise SpeedtestHistoryError("測定結果の形式が不正です。")

    try:
        test_id = str(uuid.UUID(str(payload.get("test_id") or "")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise SpeedtestHistoryError("測定IDが不正です。") from exc

    raw_ping = payload.get("ping_samples_ms")
    if not isinstance(raw_ping, list) or len(raw_ping) != SPEEDTEST_PING_SAMPLE_COUNT:
        raise SpeedtestHistoryError("Pingの測定数が一致しません。")
    ping_samples = [
        _number(value, f"Ping {index}", minimum=0.01, maximum=120_000)
        for index, value in enumerate(raw_ping, start=1)
    ]

    raw_uploads = payload.get("upload_samples")
    expected_count = len(SPEEDTEST_UPLOAD_SIZES_MB) * SPEEDTEST_UPLOAD_ROUNDS
    if not isinstance(raw_uploads, list) or len(raw_uploads) != expected_count:
        raise SpeedtestHistoryError("アップロードの測定数が一致しません。")

    upload_samples: list[dict[str, Any]] = []
    combinations: set[tuple[int, int]] = set()
    for sample_no, raw in enumerate(raw_uploads, start=1):
        if not isinstance(raw, dict):
            raise SpeedtestHistoryError("アップロード測定値の形式が不正です。")
        try:
            round_no = int(raw.get("round_no"))
            size_mb = int(raw.get("size_mb"))
            bytes_sent = int(raw.get("bytes"))
        except (TypeError, ValueError) as exc:
            raise SpeedtestHistoryError("アップロード測定値が不正です。") from exc
        if round_no not in range(1, SPEEDTEST_UPLOAD_ROUNDS + 1):
            raise SpeedtestHistoryError("測定回数が不正です。")
        if size_mb not in SPEEDTEST_UPLOAD_SIZES_MB:
            raise SpeedtestHistoryError("測定サイズが不正です。")
        if bytes_sent != size_mb * MEBIBYTE:
            raise SpeedtestHistoryError("送信バイト数が測定サイズと一致しません。")
        combination = (round_no, size_mb)
        if combination in combinations:
            raise SpeedtestHistoryError("アップロード測定値が重複しています。")
        combinations.add(combination)

        duration_ms = _number(
            raw.get("duration_ms"),
            "ブラウザ側の送信時間",
            minimum=0.01,
            maximum=1_200_000,
        )
        raw_server_elapsed = raw.get("server_elapsed_ms")
        server_elapsed_ms = None
        if raw_server_elapsed is not None:
            server_elapsed_ms = _number(
                raw_server_elapsed,
                "サーバー側の受信時間",
                minimum=0,
                maximum=1_200_000,
            )
        retried = raw.get("retried", False)
        if not isinstance(retried, bool):
            raise SpeedtestHistoryError("再試行情報が不正です。")
        mbps = (bytes_sent * 8) / (duration_ms / 1000) / 1_000_000
        upload_samples.append(
            {
                "sample_no": sample_no,
                "round_no": round_no,
                "size_mb": size_mb,
                "bytes": bytes_sent,
                "duration_ms": duration_ms,
                "server_elapsed_ms": server_elapsed_ms,
                "mbps": mbps,
                "retried": retried,
            }
        )

    total_elapsed_ms = _number(
        payload.get("total_elapsed_ms"),
        "測定時間",
        minimum=0.01,
        maximum=3_600_000,
    )
    total_bytes = sum(sample["bytes"] for sample in upload_samples)
    total_upload_ms = sum(sample["duration_ms"] for sample in upload_samples)
    overall_mbps = (total_bytes * 8) / (total_upload_ms / 1000) / 1_000_000
    ping_median_ms = float(statistics.median(ping_samples))
    jitter_ms = sum(
        abs(ping_samples[index] - ping_samples[index - 1])
        for index in range(1, len(ping_samples))
    ) / (len(ping_samples) - 1)

    return {
        "test_id": test_id,
        "ping_samples": ping_samples,
        "upload_samples": upload_samples,
        "overall_mbps": overall_mbps,
        "ping_median_ms": ping_median_ms,
        "jitter_ms": jitter_ms,
        "estimated_500mb_seconds": (500 * 8) / overall_mbps,
        "total_elapsed_ms": total_elapsed_ms,
        "total_bytes": total_bytes,
        "retry_count": sum(1 for sample in upload_samples if sample["retried"]),
    }


def _purge_expired(cur, *, now_utc: datetime) -> int:
    cutoff = (now_utc - timedelta(days=SPEEDTEST_HISTORY_RETENTION_DAYS)).replace(tzinfo=None)
    cur.execute("DELETE FROM speedtest_results WHERE completed_at_utc < %s", (cutoff,))
    return int(cur.rowcount or 0)


def record_speedtest_result(payload: Any, *, ip_address: str) -> dict[str, Any]:
    """Store one completed test atomically; repeated test IDs are idempotent."""
    result = normalize_speedtest_result(payload)
    ensure_speedtest_history_schema()
    completed_at = datetime.now(timezone.utc)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT id, completed_at_utc FROM speedtest_results WHERE test_id = %s", (result["test_id"],))
        existing = cur.fetchone()
        if existing:
            return {
                "id": int(existing["id"]),
                "created": False,
                "completed_at_utc": existing["completed_at_utc"],
            }

        _purge_expired(cur, now_utc=completed_at)
        cur.execute(
            """
            INSERT INTO speedtest_results
              (test_id, completed_at_utc, ip_address, overall_mbps,
               ping_median_ms, jitter_ms, estimated_500mb_seconds,
               total_elapsed_ms, total_bytes, retry_count, result_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
            """,
            (
                result["test_id"], completed_at.replace(tzinfo=None), ip_address[:64],
                result["overall_mbps"], result["ping_median_ms"], result["jitter_ms"],
                result["estimated_500mb_seconds"], result["total_elapsed_ms"],
                result["total_bytes"], result["retry_count"],
            ),
        )
        result_id = int(cur.lastrowid)
        cur.executemany(
            """
            INSERT INTO speedtest_ping_samples (result_id, sample_no, duration_ms)
            VALUES (%s, %s, %s)
            """,
            [(result_id, index, value) for index, value in enumerate(result["ping_samples"], start=1)],
        )
        cur.executemany(
            """
            INSERT INTO speedtest_upload_samples
              (result_id, sample_no, round_no, size_mb, bytes_sent,
               client_duration_ms, server_elapsed_ms, mbps, retried)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    result_id, sample["sample_no"], sample["round_no"], sample["size_mb"],
                    sample["bytes"], sample["duration_ms"], sample["server_elapsed_ms"],
                    sample["mbps"], int(sample["retried"]),
                )
                for sample in result["upload_samples"]
            ],
        )
        db.commit()
        return {"id": result_id, "created": True, "completed_at_utc": completed_at.replace(tzinfo=None)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _jst_text(value: datetime | None) -> str:
    if not value:
        return "-"
    utc_value = value.replace(tzinfo=timezone.utc)
    return utc_value.astimezone(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")


def _network_info(ip_address: str) -> dict[str, str]:
    """Use the same cached RDAP/WHOIS result and display priority as access logs."""
    try:
        info = get_netinfo(ip_address) or {}
    except Exception:
        info = {}
    provider = str(
        info.get("org")
        or info.get("asname")
        or info.get("netname")
        or "不明"
    ).strip()
    country = str(info.get("country") or "").strip()
    if country == "不明":
        country = ""
    return {"provider_name": provider or "不明", "country": country}


def _enrich_network_info(rows: list[dict[str, Any]]) -> None:
    unique_ips = list(dict.fromkeys(str(row.get("ip_address") or "") for row in rows))
    unique_ips = [ip for ip in unique_ips if ip]
    if not unique_ips:
        return
    workers = min(8, len(unique_ips))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        resolved = dict(zip(unique_ips, executor.map(_network_info, unique_ips)))
    for row in rows:
        info = resolved.get(str(row.get("ip_address") or ""), {})
        row["provider_name"] = info.get("provider_name") or "不明"
        row["country"] = info.get("country") or ""


def list_speedtest_history(*, limit: int = SPEEDTEST_HISTORY_LIST_LIMIT) -> list[dict[str, Any]]:
    """Return recent results with raw samples and per-size summaries."""
    ensure_speedtest_history_schema()
    safe_limit = max(1, min(int(limit), SPEEDTEST_HISTORY_LIST_LIMIT))
    now_utc = datetime.now(timezone.utc)
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        _purge_expired(cur, now_utc=now_utc)
        db.commit()
        cur.execute(
            """
            SELECT id, test_id, completed_at_utc, ip_address, overall_mbps,
                   ping_median_ms, jitter_ms, estimated_500mb_seconds,
                   total_elapsed_ms, total_bytes, retry_count
              FROM speedtest_results
             ORDER BY completed_at_utc DESC, id DESC
             LIMIT %s
            """,
            (safe_limit,),
        )
        rows = cur.fetchall()
        if not rows:
            return []

        result_ids = [int(row["id"]) for row in rows]
        placeholders = ",".join(["%s"] * len(result_ids))
        cur.execute(
            f"""
            SELECT result_id, sample_no, duration_ms
              FROM speedtest_ping_samples
             WHERE result_id IN ({placeholders})
             ORDER BY result_id, sample_no
            """,
            tuple(result_ids),
        )
        ping_by_result: dict[int, list[dict[str, Any]]] = {}
        for sample in cur.fetchall():
            ping_by_result.setdefault(int(sample["result_id"]), []).append(sample)

        cur.execute(
            f"""
            SELECT result_id, sample_no, round_no, size_mb, bytes_sent,
                   client_duration_ms, server_elapsed_ms, mbps, retried
              FROM speedtest_upload_samples
             WHERE result_id IN ({placeholders})
             ORDER BY result_id, sample_no
            """,
            tuple(result_ids),
        )
        upload_by_result: dict[int, list[dict[str, Any]]] = {}
        for sample in cur.fetchall():
            sample["retried"] = bool(sample["retried"])
            upload_by_result.setdefault(int(sample["result_id"]), []).append(sample)

        for row in rows:
            result_id = int(row["id"])
            row["completed_at_jst"] = _jst_text(row["completed_at_utc"])
            row["ping_samples"] = ping_by_result.get(result_id, [])
            row["upload_samples"] = upload_by_result.get(result_id, [])
            size_summaries = []
            for size_mb in SPEEDTEST_UPLOAD_SIZES_MB:
                samples = [sample for sample in row["upload_samples"] if int(sample["size_mb"]) == size_mb]
                speeds = [float(sample["mbps"]) for sample in samples]
                if speeds:
                    size_summaries.append(
                        {
                            "size_mb": size_mb,
                            "median_mbps": statistics.median(speeds),
                            "minimum_mbps": min(speeds),
                            "maximum_mbps": max(speeds),
                        }
                    )
            row["size_summaries"] = size_summaries
        _enrich_network_info(rows)
        return rows
    finally:
        db.close()
