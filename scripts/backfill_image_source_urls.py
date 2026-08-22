from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

sys.path[:0] = ["/mnt/mfu", "/mnt/mfu/app"]

from utils.db import get_db


JOB_WINDOW = timedelta(minutes=15)
SUPPORTED_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "threads.com",
    "www.threads.com",
    "threads.net",
    "www.threads.net",
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
}


def parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)


def supported_url(value: object) -> str:
    candidate = str(value or "").strip()
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in SUPPORTED_HOSTS:
        return ""
    return candidate


def canonical_url(job: dict) -> str:
    source = str(job.get("source") or "").lower()
    identifier = str(job.get("identifier") or "").strip()
    urls = [supported_url(value) for value in job.get("urls") or []]
    urls = [value for value in urls if value]
    if source == "instagram":
        matching = [value for value in urls if f"/{identifier}/" in value]
        if matching:
            return matching[0]
        return f"https://www.instagram.com/p/{identifier}/" if identifier else ""
    if source == "threads":
        matching = [value for value in urls if identifier in value]
        if matching:
            return matching[0]
        return f"https://www.threads.com/t/{identifier}" if identifier else ""
    if source == "x" and identifier:
        return f"https://x.com/i/status/{identifier}"
    return ""


def load_jobs(log_path: Path) -> tuple[list[dict], list[dict]]:
    jobs: dict[str, dict] = defaultdict(
        lambda: {
            "job_id": "",
            "source": "",
            "identifier": "",
            "created": None,
            "done": None,
            "sizes": Counter(),
            "urls": [],
            "kind": "image",
        }
    )
    with log_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
                event_time = parse_time(str(event.get("ts") or ""))
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            job_id = str(event.get("job_id") or "")
            if not job_id:
                continue
            job = jobs[job_id]
            job["job_id"] = job_id
            if event.get("source"):
                job["source"] = str(event["source"]).lower()
            identifier = event.get("shortcode")
            if identifier:
                job["identifier"] = str(identifier)
            name = str(event.get("event") or "")
            if name in {"job_created", "video_job_worker_start"}:
                job["created"] = job.get("created") or event_time
            if name == "video_job_worker_start":
                job["kind"] = "video"
            if name == "preview_download_done" and event.get("bytes") is not None:
                try:
                    job["sizes"][int(event["bytes"])] += 1
                except (TypeError, ValueError):
                    pass
            if name == "job_done":
                job["done"] = event_time
                job["kind"] = "image"
            elif name == "video_job_done":
                job["done"] = event_time
                job["kind"] = "video"
            if event.get("url"):
                job["urls"].append(str(event["url"]))

    image_jobs = []
    video_jobs = []
    for job in jobs.values():
        job["source_url"] = canonical_url(job)
        if not job.get("done") or not job.get("source_url"):
            continue
        if job.get("kind") == "video":
            video_jobs.append(job)
        elif job.get("sizes"):
            image_jobs.append(job)

    image_jobs.sort(key=lambda item: item.get("created") or item["done"])
    for index, job in enumerate(image_jobs):
        end = job["done"] + JOB_WINDOW
        for following in image_jobs[index + 1 :]:
            following_created = following.get("created")
            if following_created and following_created > job["done"]:
                end = min(end, following_created)
                break
        job["window_end"] = end
    return image_jobs, video_jobs


def candidate_urls_for_image(row: dict, jobs: list[dict]) -> list[tuple[str, str]]:
    created = row["created_at"]
    size = int(row["file_size"])
    candidates = []
    for job in jobs:
        if job["done"] <= created <= job["window_end"] and job["sizes"].get(size, 0):
            candidates.append((job["source_url"], job["job_id"]))
    return candidates


def candidate_urls_for_video(row: dict, jobs: list[dict]) -> list[tuple[str, str]]:
    created = row["created_at"]
    name = str(row["display_name"] or "")
    candidates = []
    for job in jobs:
        identifier = str(job.get("identifier") or "")
        if not identifier or identifier not in name:
            continue
        if job["done"] <= created <= job["done"] + JOB_WINDOW:
            candidates.append((job["source_url"], job["job_id"]))
    return candidates


def build_plan(rows: list[dict], image_jobs: list[dict], video_jobs: list[dict]) -> tuple[list[dict], int]:
    plan = []
    ambiguous = 0
    for row in rows:
        if str(row.get("media_type") or "") == "video":
            candidates = candidate_urls_for_video(row, video_jobs)
            reason = "video_identifier_and_time"
        else:
            candidates = candidate_urls_for_image(row, image_jobs)
            reason = "image_size_and_time"
        unique_urls = sorted({url for url, _ in candidates if url})
        if len(unique_urls) != 1:
            if len(unique_urls) > 1:
                ambiguous += 1
            continue
        matching_job_ids = sorted({job_id for url, job_id in candidates if url == unique_urls[0]})
        plan.append(
            {
                "id": int(row["id"]),
                "file_uuid": row["file_uuid"],
                "display_name": row["display_name"],
                "created_at": row["created_at"].isoformat(sep=" "),
                "source_url": unique_urls[0],
                "job_ids": matching_job_ids,
                "reason": reason,
            }
        )
    return plan, ambiguous


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, default=Path("/mnt/mfu/logs/instagram_fetch.log"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    image_jobs, video_jobs = load_jobs(args.log)
    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SHOW COLUMNS FROM image_viewer_files")
        columns = {row["Field"] for row in cursor.fetchall()}
        has_source_url = "source_url" in columns
        source_filter = "AND (source_url IS NULL OR source_url = '')" if has_source_url else ""
        cursor.execute(
            "SELECT id, file_uuid, display_name, media_type, file_size, created_at "
            "FROM image_viewer_files WHERE status = 'active' " + source_filter + " ORDER BY created_at"
        )
        rows = list(cursor.fetchall() or [])
        plan, ambiguous = build_plan(rows, image_jobs, video_jobs)
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "mode": "apply" if args.apply else "dry-run",
            "log": str(args.log),
            "image_jobs": len(image_jobs),
            "video_jobs": len(video_jobs),
            "eligible_files": len(rows),
            "matched_files": len(plan),
            "ambiguous_files": ambiguous,
            "unmatched_files": len(rows) - len(plan) - ambiguous,
            "mappings": plan,
        }
        if args.apply:
            if not has_source_url:
                raise RuntimeError("source_url column is missing; apply the database migration first")
            for item in plan:
                cursor.execute(
                    "UPDATE image_viewer_files SET source_url = %s "
                    "WHERE id = %s AND (source_url IS NULL OR source_url = '')",
                    (item["source_url"], item["id"]),
                )
            conn.commit()
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "mappings"}, ensure_ascii=False))
        print(json.dumps({"sample": plan[:10]}, ensure_ascii=False, default=str))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
