# -*- coding: utf-8 -*-
"""
受領書PDFを新アーカイブルートへ移行する運用スクリプト。
"""

import argparse
import logging
import os
import shutil
from datetime import datetime

from utils.db import get_db

DEFAULT_OLD_ROOT = (
    os.environ.get("MFU_RECEIPTS_LEGACY_ROOT")
    or os.environ.get("MFU_RECEIPTS_ROOT")
    or "/mnt/mfu/app/receipts"
)
DEFAULT_NEW_ROOT = os.environ.get("MFU_RECEIPTS_ARCHIVE_ROOT", "/mnt/mfu/receipts_pdf_archive")


def _sha256_file(path: str) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _collect_tree_stats(root: str) -> tuple[int, int]:
    total_files = 0
    total_bytes = 0
    for base, _, files in os.walk(root):
        for name in files:
            total_files += 1
            total_bytes += os.path.getsize(os.path.join(base, name))
    return total_files, total_bytes


def _tree_matches(src: str, dst: str) -> bool:
    return _collect_tree_stats(src) == _collect_tree_stats(dst)


def _iter_receipt_dirs(root: str):
    if not os.path.isdir(root):
        return []
    entries = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if os.path.isdir(path):
            entries.append((name, path))
    return entries


def _update_db_paths(cur, receipt_no: str, old_root: str, new_root: str, dry_run: bool) -> int:
    cur.execute(
        """
        SELECT rv.id, rv.original_pdf_path, rv.final_pdf_path
        FROM receipt_versions rv
        JOIN receipts r ON rv.receipt_id = r.id
        WHERE r.receipt_no = %s
        """,
        (receipt_no,),
    )
    rows = cur.fetchall()
    updated = 0
    for row in rows:
        row_id = row["id"]
        original = row["original_pdf_path"] or ""
        final = row["final_pdf_path"] or ""
        new_original = original
        new_final = final
        if original.startswith(old_root):
            new_original = new_root + original[len(old_root):]
        if final.startswith(old_root):
            new_final = new_root + final[len(old_root):]

        if new_original != original or new_final != final:
            if dry_run:
                logging.info(
                    "[dry-run] DB更新: receipt_no=%s version_id=%s original=%s final=%s",
                    receipt_no,
                    row_id,
                    new_original,
                    new_final,
                )
            else:
                cur.execute(
                    """
                    UPDATE receipt_versions
                    SET original_pdf_path = %s, final_pdf_path = %s
                    WHERE id = %s
                    """,
                    (new_original, new_final, row_id),
                )
            updated += 1
    return updated


def _verify_receipt(cur, receipt_no: str, new_root: str) -> tuple[int, int]:
    cur.execute(
        """
        SELECT rv.id, rv.original_pdf_path, rv.final_pdf_path, rv.hash_original, rv.hash_final
        FROM receipt_versions rv
        JOIN receipts r ON rv.receipt_id = r.id
        WHERE r.receipt_no = %s
        """,
        (receipt_no,),
    )
    rows = cur.fetchall()
    missing = 0
    hash_mismatch = 0
    for row in rows:
        original = row["original_pdf_path"] or ""
        final = row["final_pdf_path"] or ""
        for label, path, expected in (
            ("original", original, row["hash_original"]),
            ("final", final, row["hash_final"]),
        ):
            if not path:
                continue
            if not os.path.exists(path) and path.startswith(new_root):
                logging.error("ファイル未検出: receipt_no=%s %s=%s", receipt_no, label, path)
                missing += 1
                continue
            if expected:
                actual = _sha256_file(path)
                if actual != expected:
                    logging.error(
                        "ハッシュ不一致: receipt_no=%s %s=%s expected=%s actual=%s",
                        receipt_no,
                        label,
                        path,
                        expected,
                        actual,
                    )
                    hash_mismatch += 1
    return missing, hash_mismatch


def _archive_old_root(old_root: str, dry_run: bool) -> str | None:
    if not os.path.isdir(old_root):
        return None
    suffix = datetime.now().strftime("%Y%m%d")
    archived = f"{old_root}__migrated_{suffix}"
    if dry_run:
        logging.info("[dry-run] 旧ルート退避: %s -> %s", old_root, archived)
        return archived
    shutil.move(old_root, archived)
    return archived


def main() -> int:
    parser = argparse.ArgumentParser(description="受領書PDFを新アーカイブルートへ移行します。")
    parser.add_argument("--old-root", default=DEFAULT_OLD_ROOT, help="旧ルート (default: %(default)s)")
    parser.add_argument("--new-root", default=DEFAULT_NEW_ROOT, help="新ルート (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="dry-run (計画のみ表示)")
    parser.add_argument(
        "--archive-old-root",
        action="store_true",
        help="移行完了後に旧ルートを退避ディレクトリへリネーム",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    old_root = args.old_root
    new_root = args.new_root

    logging.info("旧ルート: %s", old_root)
    logging.info("新ルート: %s", new_root)
    logging.info("dry-run: %s", args.dry_run)

    receipt_dirs = _iter_receipt_dirs(old_root)
    if not receipt_dirs:
        logging.warning("旧ルートに移行対象がありません。")
        return 0

    db = get_db()
    cur = db.cursor(dictionary=True)

    migrated = 0
    skipped = 0
    conflicts = 0
    db_updates = 0
    missing_files = 0
    hash_mismatches = 0

    try:
        for receipt_no, src_dir in receipt_dirs:
            dst_dir = os.path.join(new_root, receipt_no)
            if os.path.exists(dst_dir):
                if _tree_matches(src_dir, dst_dir):
                    logging.info("既存スキップ: %s", receipt_no)
                    skipped += 1
                else:
                    logging.error("差分検出のためスキップ: %s", receipt_no)
                    conflicts += 1
                db_updates += _update_db_paths(cur, receipt_no, old_root, new_root, args.dry_run)
                continue

            if args.dry_run:
                logging.info("[dry-run] 移行: %s -> %s", src_dir, dst_dir)
            else:
                logging.info("移行: %s -> %s", src_dir, dst_dir)
                shutil.copytree(src_dir, dst_dir)
            migrated += 1
            db_updates += _update_db_paths(cur, receipt_no, old_root, new_root, args.dry_run)

        if args.dry_run:
            db.rollback()
        else:
            db.commit()

        for receipt_no, _ in receipt_dirs:
            missing, mismatch = _verify_receipt(cur, receipt_no, new_root)
            missing_files += missing
            hash_mismatches += mismatch
    finally:
        db.close()

    logging.info("---- まとめ ----")
    logging.info("移行済み: %s", migrated)
    logging.info("既存スキップ: %s", skipped)
    logging.info("差分スキップ: %s", conflicts)
    logging.info("DB更新件数: %s", db_updates)
    logging.info("未検出ファイル: %s", missing_files)
    logging.info("ハッシュ不一致: %s", hash_mismatches)

    if args.archive_old_root and conflicts == 0 and missing_files == 0:
        archived = _archive_old_root(old_root, args.dry_run)
        if archived:
            logging.info("旧ルート退避先: %s", archived)

    if conflicts or missing_files or hash_mismatches:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
