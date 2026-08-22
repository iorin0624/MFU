from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from .browser_session import ETCMaintenanceError, ETCTargetPage
from .credentials import etc_browser_lock
from .parser import ETCAuthenticationRequired, is_provisional_record, parse_statement_page
from .pdf_metadata import extract_pdf_metadata
from .repository import (
    acquire_fetch_lock,
    finish_run,
    release_fetch_lock,
    reconcile_source_records,
    save_pdf,
    start_run,
    upsert_record,
)
from .tollgate_reference import enrich_record_tollgate


PDF_ROOT = Path(os.environ.get("MFU_ETC_PDF_ROOT", "/mnt/mfu/etc_certificates"))
PAGE_URL = "/etc/R?funccode=1013000000&nextfunc=1013100000&pageNo={page}"
PDF_URL = "/etc/R?funccode=1013000000&nextfunc=1013600000"
_LOGGER = logging.getLogger(__name__)


def _month_url(statement_month: str) -> str:
    return f"{ETC_LIST_URL}&taisyoYM={statement_month}"


def _safe_pdf_path(record: dict) -> Path:
    digest = hashlib.sha256(record["transaction_key"].encode("utf-8")).hexdigest()[:16]
    used_at = record["used_at"]
    folder = PDF_ROOT / record["statement_month"]
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(PDF_ROOT, 0o700)
        os.chmod(folder, 0o700)
    except OSError:
        pass
    return folder / f"etc_{used_at:%Y%m%d_%H%M}_{digest}.pdf"


def _download_pdf(browser: ETCTargetPage, form_token: str, record: dict) -> bytes:
    content = browser.download_pdf(record["transaction_key"], form_token)
    if not content.startswith(b"%PDF-"):
        raise RuntimeError("利用証明書がPDFではありません。ETC画面を確認してください。")
    return content


def _stage_pdf_bytes(record: dict, content: bytes) -> tuple[Path, Path, str]:
    target = _safe_pdf_path(record)
    temporary = target.with_suffix(".pdf.part")
    temporary.write_bytes(content)
    os.chmod(temporary, 0o600)
    digest = hashlib.sha256(content).hexdigest()
    return target, temporary, digest


def _pdf_metadata(path: Path, record: dict) -> dict:
    try:
        return extract_pdf_metadata(str(path))
    except Exception:
        if not is_provisional_record(record):
            raise
        _LOGGER.info(
            "ETC provisional PDF metadata is not final yet: transaction_key=%s path=%s",
            record.get("transaction_key"),
            path,
            exc_info=True,
        )
        return {"registration_number": None, "issuer_name": None}


def _discard_replaced_pdf(previous_path: str, current_path: Path) -> bool:
    if not previous_path:
        return False
    root = PDF_ROOT.resolve()
    old_path = Path(previous_path).resolve()
    new_path = current_path.resolve()
    if old_path == new_path:
        return False
    try:
        old_path.relative_to(root)
    except ValueError:
        _LOGGER.warning("Refusing to remove ETC PDF outside storage root: %s", old_path)
        return False
    if not old_path.is_file():
        return False
    old_path.unlink()
    return True


def fetch_month(statement_month: str, *, force_record_ids: set[int] | None = None) -> dict:
    if not re.fullmatch(r"20\d{4}", statement_month or ""):
        raise ValueError("対象月はYYYYMM形式で指定してください。")
    lock_db = acquire_fetch_lock()
    if lock_db is None:
        raise RuntimeError("別のETC取得処理が実行中です。")
    run_id = start_run(statement_month)
    found = downloaded = skipped = 0
    new_count = updated_count = finalized_count = restored_count = 0
    changed_record_ids: set[int] = set()
    force_ids = {int(record_id) for record_id in (force_record_ids or set())}
    seen_transaction_keys = set()
    try:
        with etc_browser_lock(), ETCTargetPage() as browser:
            browser.open_statement_month(statement_month)
            first_page = parse_statement_page(browser.html(), statement_month)
            page_numbers = first_page.page_numbers
            for page_number in page_numbers:
                if page_number != 1:
                    browser.go_to_page(page_number)
                page = parse_statement_page(browser.html(), statement_month)
                for record in page.records:
                    seen_transaction_keys.add(str(record.get("transaction_key") or ""))
                    found += 1
                    stored = upsert_record(record)
                    record_id = int(stored["id"])
                    is_new = bool(stored.pop("_is_new", False))
                    details_changed = bool(stored.pop("_details_changed", False))
                    became_final = bool(stored.pop("_became_final", False))
                    restored = bool(stored.pop("_restored", False))
                    if is_new:
                        new_count += 1
                    elif became_final:
                        finalized_count += 1
                    elif restored:
                        restored_count += 1
                    elif details_changed:
                        updated_count += 1
                    if is_new or details_changed or became_final or restored:
                        changed_record_ids.add(record_id)
                    try:
                        tollgate_match = enrich_record_tollgate(
                            int(stored["id"]),
                            record.get("exit_ic"),
                        )
                        if tollgate_match.get("status") != "reference_unavailable":
                            stored.update({
                                "tollgate_operator_name": tollgate_match.get("operator_name"),
                                "tollgate_road_name": tollgate_match.get("road_name"),
                                "tollgate_matched_name": tollgate_match.get("matched_name"),
                                "tollgate_match_status": tollgate_match.get("status"),
                            })
                    except Exception:
                        _LOGGER.warning(
                            "ETC料金所マスターによる管轄会社補完に失敗しました: record_id=%s exit_ic=%s",
                            stored.get("id"),
                            record.get("exit_ic"),
                            exc_info=True,
                        )
                    current_path = str(stored.get("pdf_path") or "")
                    final_pdf_incomplete = bool(
                        not is_provisional_record(record)
                        and not stored.get("invoice_registration_number")
                    )
                    if (
                        record_id not in force_ids
                        and not details_changed
                        and not final_pdf_incomplete
                        and current_path
                        and Path(current_path).is_file()
                    ):
                        skipped += 1
                        continue
                    content = _download_pdf(browser, page.form_token, record)
                    path, temporary, digest = _stage_pdf_bytes(record, content)
                    try:
                        metadata = _pdf_metadata(temporary, record)
                        temporary.replace(path)
                        save_pdf(
                            int(stored["id"]),
                            path,
                            digest,
                            registration_number=metadata["registration_number"],
                            issuer_name=metadata["issuer_name"],
                        )
                    finally:
                        temporary.unlink(missing_ok=True)
                    _discard_replaced_pdf(current_path, path)
                    downloaded += 1
                    changed_record_ids.add(record_id)
        reconciliation = reconcile_source_records(statement_month, seen_transaction_keys)
        deleted_count = int(reconciliation.get("newly_deleted") or 0)
        change_count = len(changed_record_ids) + deleted_count
        finish_run(run_id, status="success", found=found, downloaded=downloaded, skipped=skipped)
        return {
            "status": "success",
            "statement_month": statement_month,
            "found": found,
            "downloaded": downloaded,
            "skipped": skipped,
            "new_count": new_count,
            "updated_count": updated_count,
            "finalized_count": finalized_count,
            "deleted_count": deleted_count,
            "restored_count": restored_count,
            "change_count": change_count,
            "reconciliation": reconciliation,
        }
    except ETCMaintenanceError as exc:
        finish_run(run_id, status="maintenance", found=found, downloaded=downloaded, skipped=skipped, error=str(exc))
        return {
            "status": "maintenance",
            "statement_month": statement_month,
            "found": found,
            "downloaded": downloaded,
            "skipped": skipped,
            "message": str(exc),
        }
    except ETCAuthenticationRequired as exc:
        finish_run(run_id, status="auth_required", found=found, downloaded=downloaded, skipped=skipped, error=str(exc))
        raise
    except Exception as exc:
        finish_run(run_id, status="error", found=found, downloaded=downloaded, skipped=skipped, error=str(exc))
        raise
    finally:
        release_fetch_lock(lock_db)


def scheduled_months(now: datetime | None = None, months_back: int = 2) -> list[str]:
    current = now or datetime.now()
    values = []
    cursor = current.replace(day=15)
    for _ in range(max(1, months_back)):
        values.append(cursor.strftime("%Y%m"))
        cursor = (cursor.replace(day=1) - timedelta(days=1)).replace(day=15)
    return values
