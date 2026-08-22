from __future__ import annotations

import json

from .tollgate_reference import refresh_tollgate_reference


def main() -> int:
    try:
        result = refresh_tollgate_reference()
        exit_code = 0
    except Exception as exc:
        result = {"status": "error", "error": str(exc)}
        exit_code = 1
    print(json.dumps(result, ensure_ascii=False, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
