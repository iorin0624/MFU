import math
from pathlib import Path

import pytest

from app.utils.speedtest import MEBIBYTE
from app.utils.speedtest_history import SpeedtestHistoryError, normalize_speedtest_result


ROOT = Path(__file__).resolve().parents[1]


def sample_payload():
    uploads = []
    for round_no in range(1, 4):
        for size_mb in (3, 5, 10, 15):
            uploads.append(
                {
                    "round_no": round_no,
                    "size_mb": size_mb,
                    "bytes": size_mb * MEBIBYTE,
                    "duration_ms": size_mb * 10,
                    "server_elapsed_ms": size_mb * 9,
                    "retried": round_no == 2 and size_mb == 5,
                }
            )
    return {
        "test_id": "2b73e12e-144f-45bd-92e7-2595d97508e9",
        "ping_samples_ms": [10, 11, 12, 11, 13, 12, 10, 11, 12, 11],
        "total_elapsed_ms": 5000,
        "upload_samples": uploads,
    }


def test_normalize_preserves_all_raw_samples_and_calculates_totals():
    result = normalize_speedtest_result(sample_payload())

    assert len(result["ping_samples"]) == 10
    assert len(result["upload_samples"]) == 12
    assert result["total_bytes"] == 99 * MEBIBYTE
    assert result["retry_count"] == 1
    assert result["ping_median_ms"] == 11
    assert math.isclose(result["jitter_ms"], 11 / 9)
    assert result["overall_mbps"] > 0


def test_normalize_rejects_duplicate_round_and_size():
    payload = sample_payload()
    payload["upload_samples"][-1]["round_no"] = 1

    with pytest.raises(SpeedtestHistoryError, match="重複"):
        normalize_speedtest_result(payload)


def test_speedtest_template_posts_raw_samples_and_shows_history():
    template = (ROOT / "templates" / "speedtest.html").read_text(encoding="utf-8")
    app_source = (ROOT / "__init__.py").read_text(encoding="utf-8")

    assert 'fetch("/api/speedtest/result"' in template
    assert "server_elapsed_ms" in template
    assert "Ping個別値" in template
    assert '@app.post("/api/speedtest/result")' in app_source
    assert "speedtest_request_ip(request)" in app_source
