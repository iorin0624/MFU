from __future__ import annotations

import logging

from flask import current_app, has_app_context

from .fetcher import fetch_all_pages
from .repository import (
    acquire_refresh_lock,
    begin_run,
    ensure_schema,
    mark_run_failed,
    publish_snapshot,
    release_refresh_lock,
)


class RefreshAlreadyRunning(RuntimeError):
    pass


def _logger():
    return current_app.logger if has_app_context() else logging.getLogger(__name__)


def refresh_popcorn_data(*, get=None) -> dict:
    ensure_schema()
    lock_db = acquire_refresh_lock()
    if lock_db is None:
        raise RefreshAlreadyRunning("別のポップコーン情報更新が実行中です")

    run_id = begin_run()
    try:
        dataset, metadata = fetch_all_pages(get=get)
        snapshot_id, changed = publish_snapshot(run_id, dataset, metadata)
        _logger().info(
            "TDR popcorn refresh succeeded: run=%s snapshot=%s changed=%s counts=%s",
            run_id,
            snapshot_id,
            changed,
            metadata.get("counts"),
        )
        return {
            "ok": True,
            "run_id": run_id,
            "snapshot_id": snapshot_id,
            "changed": changed,
            "counts": metadata.get("counts") or {},
        }
    except Exception as exc:
        try:
            mark_run_failed(run_id, str(exc))
        except Exception:
            _logger().exception("TDR popcorn refresh failure could not be recorded")
        _logger().exception("TDR popcorn refresh failed: run=%s", run_id)
        raise
    finally:
        release_refresh_lock(lock_db)

