from __future__ import annotations

import sys
from pathlib import Path


def _setup_import_path() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))


def main() -> int:
    _setup_import_path()
    try:
        from app import create_app
        from app.shipment_tracking.services import ensure_shipment_tracking_schema, run_scheduled_checks

        app = create_app()
        with app.app_context():
            ensure_shipment_tracking_schema()
            results = run_scheduled_checks()
            ok_count = sum(1 for r in results if r["success"])
            ng_count = len(results) - ok_count
            print(f"shipment_tracking scheduled finished total={len(results)} success={ok_count} fail={ng_count}")
            for r in results:
                status = "OK" if r["success"] else "NG"
                print(f"[{status}] id={r['id']} carrier={r['carrier_code']} no={r['tracking_number']}")
            return 0
    except Exception as exc:
        print(f"shipment_tracking scheduled fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
