from __future__ import annotations

import argparse

from app import create_app

from .batch import run_batch_job


def main() -> int:
    parser = argparse.ArgumentParser(description="Register selected ETC records with freee")
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    app = create_app()
    with app.app_context():
        run_batch_job(args.job_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
