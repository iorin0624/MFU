from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

load_dotenv("/mnt/mfu/app/.env")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.image_viewer.catalog import (
    checksum_file,
    create_folder,
    list_payload,
    set_thumbnail,
    store_file,
)


MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".mp4", ".webm", ".mov", ".m4v",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy the legacy image viewer tree into the UUID catalog store."
    )
    parser.add_argument(
        "--source", default="/mnt/mfu/image_viewer_uploads", type=Path
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--skip-checksum", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.resolve()
    files = sorted(
        path for path in source.rglob("*")
        if path.is_file()
        and ".thumbs" not in path.relative_to(source).parts
        and path.suffix.lower() in MEDIA_EXTENSIONS
    )
    folders = sorted(
        {
            path.parent.relative_to(source).as_posix()
            for path in files
            if path.parent != source
        },
        key=lambda value: (value.count("/"), value.lower()),
    )
    print(f"source={source}")
    print(f"folders={len(folders)} files={len(files)} apply={args.apply}")
    if not args.apply:
        print("Dry run only. Re-run with --apply after taking a backup.")
        return 0

    existing_folders = set(list_payload()["folders"])
    for folder in folders:
        if folder in existing_folders:
            continue
        parent, _, name = folder.rpartition("/")
        create_folder(parent, name)
        existing_folders.add(folder)

    completed = 0
    for path in files:
        rel = path.relative_to(source)
        folder = "" if rel.parent.as_posix() == "." else rel.parent.as_posix()
        digest = None if args.skip_checksum else checksum_file(path)
        try:
            record = store_file(
                path,
                folder,
                display_name=path.name,
                move_source=False,
                checksum=digest,
            )
            old_thumbnail = (source / ".thumbs" / rel).with_suffix(".webp")
            if old_thumbnail.is_file():
                with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as fp:
                    temp_thumbnail = Path(fp.name)
                try:
                    shutil.copy2(old_thumbnail, temp_thumbnail)
                    set_thumbnail(record["uuid"], temp_thumbnail)
                finally:
                    temp_thumbnail.unlink(missing_ok=True)
            completed += 1
        except Exception as exc:
            print(f"ERROR {rel}: {exc}", file=sys.stderr)
            return 1
        if completed % 100 == 0:
            print(f"copied={completed}/{len(files)}", flush=True)
    print(f"completed={completed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
