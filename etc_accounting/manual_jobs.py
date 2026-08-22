from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path


MANUAL_JOB_ROOT = Path(
    os.environ.get("ETC_MANUAL_JOB_DIR", "/mnt/mfu/tmp/etc_manual_fetch_jobs")
).expanduser()
MANUAL_JOB_TTL_SECONDS = 24 * 60 * 60


def _job_path(job_id: str) -> Path:
    value = str(job_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("手動取得ジョブIDが不正です。")
    return MANUAL_JOB_ROOT / f"{value}.json"


def _write_job(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    temporary.replace(path)


def cleanup_manual_fetch_jobs() -> None:
    if not MANUAL_JOB_ROOT.is_dir():
        return
    cutoff = time.time() - MANUAL_JOB_TTL_SECONDS
    for path in MANUAL_JOB_ROOT.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def create_manual_fetch_job(statement_month: str) -> str:
    cleanup_manual_fetch_jobs()
    job_id = uuid.uuid4().hex
    now = time.time()
    _write_job(
        _job_path(job_id),
        {
            "jobId": job_id,
            "statementMonth": str(statement_month or ""),
            "status": "pending",
            "createdAt": now,
            "updatedAt": now,
        },
    )
    return job_id


def read_manual_fetch_job(job_id: str) -> dict | None:
    path = _job_path(job_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return value if isinstance(value, dict) else None


def update_manual_fetch_job(job_id: str, **fields) -> dict:
    path = _job_path(job_id)
    current = read_manual_fetch_job(job_id) or {"jobId": job_id, "createdAt": time.time()}
    current.update(fields)
    current["updatedAt"] = time.time()
    _write_job(path, current)
    return current
