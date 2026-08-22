from __future__ import annotations

import argparse
import json
import logging

from app.tdr.popcorn.service import refresh_popcorn_data


def main() -> int:
    parser = argparse.ArgumentParser(description="MFU TDR information updater")
    parser.add_argument("--source", choices=("popcorn",), default="popcorn")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.source == "popcorn":
        result = refresh_popcorn_data()
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

