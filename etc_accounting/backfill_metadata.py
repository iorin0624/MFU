from __future__ import annotations

import json

from .pdf_metadata import extract_pdf_metadata
from .parser import is_provisional_record
from .repository import list_records, update_pdf_metadata


def backfill_pdf_metadata() -> dict:
    updated = skipped = failed = 0
    errors = []
    for record in list_records(status="", limit=10000):
        if is_provisional_record(record):
            skipped += 1
            continue
        if record.get("invoice_registration_number"):
            skipped += 1
            continue
        path = str(record.get("pdf_path") or "")
        if not path:
            skipped += 1
            continue
        try:
            metadata = extract_pdf_metadata(path)
            update_pdf_metadata(
                int(record["id"]),
                metadata["registration_number"],
                metadata["issuer_name"],
            )
            updated += 1
        except Exception as exc:
            failed += 1
            errors.append({"id": int(record["id"]), "error": str(exc)[:300]})
    return {"updated": updated, "skipped": skipped, "failed": failed, "errors": errors}


def main() -> int:
    result = backfill_pdf_metadata()
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
