from __future__ import annotations

import os
import base64
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from functools import wraps
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Response, abort, jsonify, redirect, render_template, request, send_file, send_from_directory, session, stream_with_context, url_for
from PIL import Image, ImageOps
from werkzeug.utils import safe_join

from . import image_viewer_bp

try:
    import instaloader
except Exception:  # pragma: no cover - optional dependency
    instaloader = None

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
UPLOAD_ROOT = Path(os.environ.get("IMAGE_VIEWER_UPLOAD_DIR", "/mnt/mfu/image_viewer_uploads")).expanduser()
THUMB_DIR_NAME = ".thumbs"
THUMB_ROOT = UPLOAD_ROOT / THUMB_DIR_NAME
PREVIEW_ROOT = Path(os.environ.get("IMAGE_VIEWER_PREVIEW_DIR", os.path.join(os.environ.get("TMPDIR", "/tmp"), "mfu_image_viewer_instagram_previews"))).expanduser()
THUMB_JOB_ROOT = PREVIEW_ROOT / "_thumbnail_jobs"
VIDEO_JOB_ROOT = PREVIEW_ROOT / "_video_jobs"
AI_JOB_ROOT = Path(os.environ.get("IMAGE_VIEWER_AI_JOB_DIR", "/mnt/mfu/tmp/mfu_image_viewer_ai_jobs")).expanduser()
THUMB_SIZE = (360, 360)
THUMB_QUALITY = 82
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
DEFAULT_ILLUSTRATION_PROMPT = (
    "画像を詳細なアニメの美意識で再構成してください。 "
    "表情豊かな瞳、なめらかな網掛けセルの色使い、はっきりした線画を使用します。"
    "アニメのシーンに典型的な身ぶりと雰囲気で、心情と登場人物の存在を強調してください。 "
    "服とアクセサリーを参考にしてイラストを描いてください。背景は白地で、人物は全身を描いてください。 "
    "服と靴の装飾はできるだけ綺麗にこだわってください。 "
    "顔は、20代女性を生成して置き換えてください。 "
    "生成が完了したら完了したと報告をください。"
)
EMPTY_GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
_thumb_lock = threading.Lock()
_thumb_worker_running = False
_thumb_jobs: dict[str, dict] = {}
_thumb_jobs_lock = threading.Lock()
_instagram_jobs: dict[str, dict] = {}
_instagram_jobs_lock = threading.Lock()
_video_jobs: dict[str, dict] = {}
_video_jobs_lock = threading.Lock()
_ai_jobs: dict[str, dict] = {}
_ai_jobs_lock = threading.Lock()
_INSTAGRAM_JOB_TTL_SECONDS = 15 * 60
_AI_JOB_TTL_SECONDS = 6 * 60 * 60
_INSTAGRAM_PREVIEW_WORKERS = 4
_INSTAGRAM_PREVIEW_DOWNLOAD_TIMEOUT = 30


def _empty_image_response(status: int = 200) -> Response:
    return Response(EMPTY_GIF_BYTES, status=status, mimetype="image/gif", headers={"Cache-Control": "no-store"})


def _job_dir(job_id: str) -> Path:
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(job_id or ""))
    return PREVIEW_ROOT / safe_job_id


def _job_manifest_path(job_id: str) -> Path:
    return _job_dir(job_id) / "manifest.json"


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            if request.path.startswith("/image_viewer/api/") or request.accept_mimetypes.accept_json:
                return jsonify({"ok": False, "error": "ログインが必要です。"}), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapper


def _ensure_upload_root() -> None:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    THUMB_ROOT.mkdir(parents=True, exist_ok=True)
    PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    THUMB_JOB_ROOT.mkdir(parents=True, exist_ok=True)
    VIDEO_JOB_ROOT.mkdir(parents=True, exist_ok=True)
    AI_JOB_ROOT.mkdir(parents=True, exist_ok=True)


def _relative_posix(path: Path) -> str:
    return path.relative_to(UPLOAD_ROOT).as_posix()


def _is_inside_hidden_thumb_dir(path: Path) -> bool:
    try:
        path.relative_to(THUMB_ROOT)
        return True
    except ValueError:
        return False


def _thumb_path_for(image_path: Path) -> Path:
    rel = image_path.relative_to(UPLOAD_ROOT)
    return (THUMB_ROOT / rel).with_suffix(".webp")


def _thumb_rel_for(image_path: Path) -> str:
    return _thumb_path_for(image_path).relative_to(THUMB_ROOT).as_posix()


def _thumbnail_is_fresh(image_path: Path) -> bool:
    thumb_path = _thumb_path_for(image_path)
    return thumb_path.is_file() and thumb_path.stat().st_mtime >= image_path.stat().st_mtime


def _generate_thumbnail(image_path: Path) -> bool:
    thumb_path = _thumb_path_for(image_path)
    if _thumbnail_is_fresh(image_path):
        return False
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image_path) as img:
        img = ImageOps.exif_transpose(img)
        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        img.thumbnail(THUMB_SIZE, resample)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        img.save(thumb_path, "WEBP", quality=THUMB_QUALITY, method=6)
    return True


def _generate_video_thumbnail(video_path: Path) -> bool:
    thumb_path = _thumb_path_for(video_path)
    if _thumbnail_is_fresh(video_path):
        return False
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "1",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=360:360:force_original_aspect_ratio=decrease",
        "-q:v",
        "75",
        str(thumb_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    return thumb_path.is_file()


def _generate_media_thumbnail(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return _generate_video_thumbnail(path)
    return _generate_thumbnail(path)


def _iter_images(folder_value: str | None = None) -> list[Path]:
    _ensure_upload_root()
    if folder_value is not None:
        root = _target_dir_for_folder(folder_value)
        candidates = sorted(root.iterdir(), key=lambda p: p.as_posix().lower()) if root.is_dir() else []
    else:
        candidates = sorted(UPLOAD_ROOT.rglob("*"), key=lambda p: p.as_posix().lower())
    paths: list[Path] = []
    for path in candidates:
        if _is_inside_hidden_thumb_dir(path):
            continue
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
            paths.append(path)
    return paths


def _clear_thumbnail_cache(folder_value: str) -> None:
    if not folder_value:
        THUMB_ROOT.mkdir(parents=True, exist_ok=True)
        for path in THUMB_ROOT.iterdir():
            if path.is_file() and path.suffix.lower() == ".webp":
                path.unlink()
        return
    target = (THUMB_ROOT / folder_value).resolve()
    target.relative_to(THUMB_ROOT.resolve())
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)


def _extract_shortcode(value: str) -> str:
    text = (value or "").strip()
    match = re.search(r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|tv)/([^/?#]+)/?", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]+", text):
        return text
    return ""


def _extract_x_status_id(value: str) -> str:
    text = (value or "").strip()
    match = re.search(r"(?:x|twitter)\.com/[^/?#]+/status/(\d+)(?:/photo/\d+)?", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"\d{8,}", text):
        return text
    return ""


def _extract_media_identifier(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    x_status_id = _extract_x_status_id(text)
    if x_status_id:
        return "x", x_status_id
    shortcode = _extract_shortcode(text)
    if shortcode:
        return "instagram", shortcode
    return "", ""


def _guess_image_suffix(image_url: str) -> str:
    suffix = Path(urlparse(image_url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"


def _guess_video_suffix(video_url: str) -> str:
    suffix = Path(urlparse(video_url).path).suffix.lower()
    return suffix if suffix in VIDEO_EXTENSIONS else ".mp4"


def _instagram_image_items(shortcode: str) -> list[str]:
    if instaloader is None:
        raise RuntimeError("instaloader が利用できません。")
    loader = instaloader.Instaloader(
        download_videos=False,
        download_video_thumbnails=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    if post.typename == "GraphSidecar":
        return [node.display_url for node in post.get_sidecar_nodes() if not node.is_video]
    if not post.is_video:
        return [post.url]
    return []


def _x_image_items(status_id: str) -> list[str]:
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    api_url = f"https://api.fxtwitter.com/status/{status_id}"
    try:
        response = requests.get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        media = ((data.get("tweet") or {}).get("media") or {}).get("photos") or []
        urls = [str(item.get("url") or "") for item in media if item.get("url")]
        if urls:
            return urls
    except Exception:
        pass

    api_url = f"https://api.vxtwitter.com/Twitter/status/{status_id}"
    response = requests.get(api_url, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    media = data.get("media_extended") or []
    urls = []
    for item in media:
        if item.get("type") in {"image", "photo"} and item.get("url"):
            url = str(item["url"])
            if "pbs.twimg.com/media/" in url and "name=" not in url:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}name=orig"
            urls.append(url)
    return urls


def _instagram_video_items(shortcode: str) -> list[str]:
    if instaloader is None:
        raise RuntimeError("instaloader が利用できません。")
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        quiet=True,
    )
    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    urls: list[str] = []
    if post.typename == "GraphSidecar":
        for node in post.get_sidecar_nodes():
            if not getattr(node, "is_video", False):
                continue
            video_url = getattr(node, "video_url", None)
            if video_url:
                urls.append(str(video_url))
    elif post.is_video and post.video_url:
        urls.append(str(post.video_url))
    return urls


def _best_video_url_from_item(item: dict) -> str:
    if isinstance(item, str):
        return item if ".mp4" in item.lower() or "video" in item.lower() else ""
    video_info = item.get("video_info") if isinstance(item.get("video_info"), dict) else {}
    variants = item.get("variants") or item.get("video_variants") or video_info.get("variants") or []
    best_url = ""
    best_bitrate = -1
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            url = str(variant.get("url") or "")
            if not url:
                continue
            content_type = str(variant.get("content_type") or variant.get("type") or "").lower()
            if content_type and "mp4" not in content_type and "video" not in content_type:
                continue
            bitrate = int(variant.get("bitrate") or 0)
            if bitrate >= best_bitrate:
                best_bitrate = bitrate
                best_url = url
    if best_url:
        return best_url
    for key in ("url", "source", "video_url", "videoUrl", "playbackUrl"):
        url = str(item.get(key) or "")
        if url and (".mp4" in url.lower() or "video" in url.lower()):
            return url
    return ""


def _x_video_items(status_id: str) -> list[str]:
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    urls: list[str] = []
    api_url = f"https://api.fxtwitter.com/status/{status_id}"
    try:
        response = requests.get(api_url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        media = ((data.get("tweet") or {}).get("media") or {})
        candidates = []
        for key in ("videos", "all", "media"):
            value = media.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        for item in candidates:
            url = _best_video_url_from_item(item) if isinstance(item, (dict, str)) else ""
            if url:
                urls.append(url)
        if urls:
            return list(dict.fromkeys(urls))
    except Exception:
        pass

    api_url = f"https://api.vxtwitter.com/Twitter/status/{status_id}"
    response = requests.get(api_url, headers=headers, timeout=30)
    response.raise_for_status()
    data = response.json()
    media = data.get("media_extended") or []
    for item in media:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"video", "animated_gif"}:
            continue
        url = _best_video_url_from_item(item)
        if url:
            urls.append(url)
    return list(dict.fromkeys(urls))


def _media_image_items(source: str, identifier: str) -> list[str]:
    if source == "x":
        return _x_image_items(identifier)
    return _instagram_image_items(identifier)


def _media_video_items(source: str, identifier: str) -> list[str]:
    if source == "x":
        return _x_video_items(identifier)
    return _instagram_video_items(identifier)


def _instagram_image_payload(shortcode: str, items: list[str], job_id: str = "", source: str = "instagram") -> list[dict]:
    _ensure_upload_root()
    payload = []
    for idx, image_url in enumerate(items, start=1):
        suffix = _guess_image_suffix(image_url)
        item = {
            "index": idx,
            "url": image_url,
            "suffix": suffix,
            "filename": f"{source}_{shortcode}_{idx:03d}{suffix}",
        }
        if job_id:
            item["previewReady"] = False
        payload.append(item)
    return payload


def _video_payload(identifier: str, items: list[str], source: str = "instagram") -> list[dict]:
    payload = []
    for idx, video_url in enumerate(items, start=1):
        suffix = _guess_video_suffix(video_url)
        payload.append(
            {
                "index": idx,
                "url": video_url,
                "suffix": suffix,
                "filename": f"{source}_{identifier}_{idx:03d}{suffix}",
            }
        )
    return payload


def _write_instagram_job(job_id: str, job: dict) -> None:
    _ensure_upload_root()
    path = _job_manifest_path(job_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def _read_instagram_job(job_id: str) -> dict:
    with _instagram_jobs_lock:
        job = dict(_instagram_jobs.get(job_id) or {})
    if job:
        return job
    path = _job_manifest_path(job_id)
    if not path.is_file():
        return {}
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(job, dict):
        return {}
    with _instagram_jobs_lock:
        _instagram_jobs[job_id] = dict(job)
    return job


def _set_instagram_job(job_id: str, updates: dict) -> None:
    with _instagram_jobs_lock:
        job = dict(_instagram_jobs.get(job_id) or {})
        job.update(updates)
        _instagram_jobs[job_id] = job
    _write_instagram_job(job_id, job)


def _cleanup_instagram_jobs() -> None:
    now = time.time()
    with _instagram_jobs_lock:
        expired = [
            job_id
            for job_id, job in _instagram_jobs.items()
            if now - float(job.get("created_at") or now) > _INSTAGRAM_JOB_TTL_SECONDS
        ]
        for job_id in expired:
            _instagram_jobs.pop(job_id, None)
            shutil.rmtree(_job_dir(job_id), ignore_errors=True)


def _start_instagram_fetch_job(shortcode: str, source: str = "instagram") -> str:
    _cleanup_instagram_jobs()
    job_id = uuid.uuid4().hex
    _set_instagram_job(
        job_id,
        {
            "status": "pending",
            "source": source,
            "shortcode": shortcode,
            "created_at": time.time(),
            "images": [],
            "total": 0,
            "processed": 0,
            "downloaded": 0,
            "failed": 0,
            "error": "",
        },
    )

    def worker() -> None:
        try:
            items = _media_image_items(source, shortcode)
            if not items:
                raise RuntimeError("画像を取得できませんでした。")
            payload = _instagram_image_payload(shortcode, items, job_id, source)
            _set_instagram_job(
                job_id,
                {
                    "status": "downloading",
                    "images": payload,
                    "total": len(payload),
                    "processed": 0,
                    "downloaded": 0,
                    "failed": 0,
                },
            )
            downloaded = 0
            failed = 0

            def download_preview(row: tuple[int, dict]) -> tuple[int, bool, str, str]:
                idx, item = row
                image_url = str(item.get("url") or "")
                suffix = str(item.get("suffix") or _guess_image_suffix(image_url))
                preview_path = PREVIEW_ROOT / job_id / f"{idx:03d}{suffix}"
                try:
                    _download_instagram_image(image_url, preview_path)
                    return idx, True, str(preview_path), ""
                except Exception as exc:
                    return idx, False, "", str(exc)

            future_map = {}
            processed_indexes = set()
            executor = ThreadPoolExecutor(max_workers=_INSTAGRAM_PREVIEW_WORKERS)
            try:
                future_map = {
                    executor.submit(download_preview, (idx, item)): idx
                    for idx, item in enumerate(payload, start=1)
                }
                try:
                    completed_iter = as_completed(future_map, timeout=_INSTAGRAM_PREVIEW_DOWNLOAD_TIMEOUT)
                    for future in completed_iter:
                        idx, ok, cache_path, error = future.result()
                        processed_indexes.add(idx)
                        item = payload[idx - 1]
                        if ok:
                            item["previewCache"] = cache_path
                            item["previewReady"] = True
                            item.pop("previewError", None)
                            downloaded += 1
                        else:
                            item["previewReady"] = False
                            item["previewError"] = error
                            failed += 1
                        _set_instagram_job(
                            job_id,
                            {
                                "status": "downloading",
                                "images": payload,
                                "processed": downloaded + failed,
                                "downloaded": downloaded,
                                "failed": failed,
                            },
                        )
                except TimeoutError:
                    pass

                for future, idx in future_map.items():
                    item = payload[idx - 1]
                    if idx in processed_indexes:
                        continue
                    if future.done():
                        idx, ok, cache_path, error = future.result()
                        if ok:
                            item["previewCache"] = cache_path
                            item["previewReady"] = True
                            item.pop("previewError", None)
                            downloaded += 1
                        else:
                            item["previewReady"] = False
                            item["previewError"] = error
                            failed += 1
                        processed_indexes.add(idx)
                        continue
                    future.cancel()
                    item["previewReady"] = False
                    item["previewError"] = "preview download timeout"
                    failed += 1
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            _set_instagram_job(
                job_id,
                {
                    "status": "downloading",
                    "images": payload,
                    "processed": len(payload),
                    "downloaded": downloaded,
                    "failed": failed,
                },
            )
            if downloaded <= 0:
                raise RuntimeError("画像をダウンロードできませんでした。")
            _set_instagram_job(job_id, {"status": "done", "images": payload, "processed": len(payload), "downloaded": downloaded, "failed": failed})
        except Exception as exc:
            _set_instagram_job(job_id, {"status": "error", "error": str(exc)})

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _video_job_path(job_id: str) -> Path:
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(job_id or ""))
    return VIDEO_JOB_ROOT / f"{safe_job_id}.json"


def _write_video_job(job_id: str, job: dict) -> None:
    _ensure_upload_root()
    path = _video_job_path(job_id)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def _read_video_job(job_id: str) -> dict:
    with _video_jobs_lock:
        job = dict(_video_jobs.get(job_id) or {})
    if job:
        return job
    path = _video_job_path(job_id)
    if not path.is_file():
        return {}
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(job, dict):
        return {}
    with _video_jobs_lock:
        _video_jobs[job_id] = dict(job)
    return job


def _set_video_job(job_id: str, updates: dict) -> None:
    with _video_jobs_lock:
        job = dict(_video_jobs.get(job_id) or {})
        job.update(updates)
        _video_jobs[job_id] = job
    _write_video_job(job_id, job)


def _cleanup_video_jobs() -> None:
    now = time.time()
    with _video_jobs_lock:
        expired = [
            job_id
            for job_id, job in _video_jobs.items()
            if now - float(job.get("created_at") or now) > _INSTAGRAM_JOB_TTL_SECONDS
        ]
        for job_id in expired:
            _video_jobs.pop(job_id, None)
            _video_job_path(job_id).unlink(missing_ok=True)


def _start_video_fetch_job(identifier: str, source: str = "instagram") -> str:
    _cleanup_video_jobs()
    job_id = uuid.uuid4().hex
    _set_video_job(
        job_id,
        {
            "status": "pending",
            "source": source,
            "identifier": identifier,
            "created_at": time.time(),
            "videos": [],
            "error": "",
        },
    )

    def worker() -> None:
        try:
            items = _media_video_items(source, identifier)
            if not items:
                raise RuntimeError("動画を取得できませんでした。")
            payload = _video_payload(identifier, items, source)
            _set_video_job(job_id, {"status": "done", "videos": payload})
        except Exception as exc:
            _set_video_job(job_id, {"status": "error", "error": str(exc)})

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _ai_job_path(job_id: str) -> Path:
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(job_id or ""))
    return AI_JOB_ROOT / f"{safe_job_id}.json"


def _write_ai_job(job_id: str, job: dict) -> None:
    _ensure_upload_root()
    path = _ai_job_path(job_id)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def _read_ai_job(job_id: str) -> dict:
    with _ai_jobs_lock:
        job = dict(_ai_jobs.get(job_id) or {})
    if job:
        return job
    path = _ai_job_path(job_id)
    if not path.is_file():
        return {}
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(job, dict):
        return {}
    with _ai_jobs_lock:
        _ai_jobs[job_id] = dict(job)
    return job


def _set_ai_job(job_id: str, updates: dict) -> None:
    with _ai_jobs_lock:
        job = dict(_ai_jobs.get(job_id) or {})
        job.update(updates)
        job["updated_at"] = time.time()
        _ai_jobs[job_id] = job
    _write_ai_job(job_id, job)


def _cleanup_ai_jobs() -> None:
    now = time.time()
    with _ai_jobs_lock:
        expired = [
            job_id
            for job_id, job in _ai_jobs.items()
            if now - float(job.get("created_at") or now) > _AI_JOB_TTL_SECONDS
        ]
        for job_id in expired:
            job = _ai_jobs.get(job_id) or {}
            result_cache = Path(str((job.get("generated") or {}).get("previewCache") or ""))
            if result_cache:
                try:
                    result_cache.resolve().relative_to(AI_JOB_ROOT.resolve())
                    result_cache.unlink(missing_ok=True)
                except Exception:
                    pass
            _ai_jobs.pop(job_id, None)
            _ai_job_path(job_id).unlink(missing_ok=True)


def _normalized_image_file_for_openai(source_path: Path) -> Path:
    target_path = PREVIEW_ROOT / f"openai_input_{uuid.uuid4().hex}.png"
    with Image.open(source_path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.save(target_path, "PNG")
    return target_path


def _extract_image_response_bytes(response) -> bytes:
    data = getattr(response, "data", None) or []
    if not data:
        raise RuntimeError("OpenAIから画像データを取得できませんでした。")
    item = data[0]
    b64_json = getattr(item, "b64_json", None)
    if b64_json:
        return base64.b64decode(b64_json)
    url = getattr(item, "url", None)
    if url:
        download = requests.get(url, headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=90)
        download.raise_for_status()
        return download.content
    raise RuntimeError("OpenAIから画像データを取得できませんでした。")


def _image_model_value(value: str) -> str:
    model = (value or "").strip() or os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1.5")
    allowed = {"gpt-image-1.5", "gpt-image-2", "gpt-image-1", "gpt-image-1-mini"}
    return model if model in allowed else "gpt-image-1.5"


def _generate_illustration_from_image(source_path: Path, prompt: str, quality: str = "medium", model: str = "") -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が未設定です。")
    model_value = _image_model_value(model)
    quality_value = quality if quality in {"low", "medium", "high", "auto"} else os.getenv("OPENAI_IMAGE_QUALITY", "medium")
    if quality_value not in {"low", "medium", "high", "auto"}:
        quality_value = "medium"
    prompt_text = (prompt or "").strip() or DEFAULT_ILLUSTRATION_PROMPT
    normalized_path = _normalized_image_file_for_openai(source_path)
    result_path = AI_JOB_ROOT / f"illustration_{uuid.uuid4().hex}.png"
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        with open(normalized_path, "rb") as image_fp:
            response = client.images.edit(
                model=model_value,
                image=image_fp,
                prompt=prompt_text,
                size="1024x1536",
                quality=quality_value,
                output_format="png",
                timeout=1800,
            )
        result_path.write_bytes(_extract_image_response_bytes(response))
    finally:
        normalized_path.unlink(missing_ok=True)
    return {"previewCache": str(result_path), "model": model_value, "quality": quality_value}


def _start_illustration_job(source_rel: str, prompt: str, folder: str, quality: str = "medium", model: str = "") -> str:
    _cleanup_ai_jobs()
    source_path = _target_path_for_rel(source_rel)
    if not source_path.is_file() or source_path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise ValueError("画像ファイルを選択してください。")
    _target_dir_for_folder(folder)
    job_id = uuid.uuid4().hex
    _set_ai_job(
        job_id,
        {
            "status": "pending",
            "created_at": time.time(),
            "source": source_rel,
            "folder": folder,
            "quality": quality,
            "model": _image_model_value(model),
            "saved": None,
            "error": "",
        },
    )

    def worker() -> None:
        try:
            _set_ai_job(job_id, {"status": "running"})
            generated = _generate_illustration_from_image(source_path, prompt, quality, model)
            _set_ai_job(job_id, {"status": "done", "generated": generated})
        except Exception as exc:
            _set_ai_job(job_id, {"status": "error", "error": str(exc)})

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _safe_relative_folder(value: str) -> Path:
    text = str(value or "").replace("\\", "/").strip().strip("/")
    parts = [part for part in text.split("/") if part and part not in {".", ".."}]
    return Path(*parts) if parts else Path()


def _target_dir_for_folder(folder_value: str) -> Path:
    target_dir = UPLOAD_ROOT / _safe_relative_folder(folder_value or "")
    target_dir.resolve().relative_to(UPLOAD_ROOT.resolve())
    return target_dir


def _target_path_for_rel(path_value: str) -> Path:
    target_path = UPLOAD_ROOT / _safe_relative_folder(path_value or "")
    resolved = target_path.resolve()
    resolved.relative_to(UPLOAD_ROOT.resolve())
    if _is_inside_hidden_thumb_dir(resolved):
        raise ValueError("サムネイル管理フォルダーは操作できません。")
    return resolved


def _thumb_path_for_rel(path_value: str) -> Path:
    return (THUMB_ROOT / _safe_relative_folder(path_value or "")).with_suffix(".webp")


def _thumb_dir_for_rel(path_value: str) -> Path:
    return THUMB_ROOT / _safe_relative_folder(path_value or "")


def _delete_thumb_for_path(path: Path) -> None:
    try:
        rel = path.relative_to(UPLOAD_ROOT).as_posix()
    except ValueError:
        return
    if path.is_dir():
        shutil.rmtree(_thumb_dir_for_rel(rel), ignore_errors=True)
    else:
        _thumb_path_for_rel(rel).unlink(missing_ok=True)


def _move_thumb_for_path(source: Path, target: Path, is_dir: bool) -> None:
    try:
        source_rel = source.relative_to(UPLOAD_ROOT).as_posix()
        target_rel = target.relative_to(UPLOAD_ROOT).as_posix()
    except ValueError:
        return
    source_thumb = _thumb_dir_for_rel(source_rel) if is_dir else _thumb_path_for_rel(source_rel)
    target_thumb = _thumb_dir_for_rel(target_rel) if is_dir else _thumb_path_for_rel(target_rel)
    if not source_thumb.exists():
        return
    target_thumb.parent.mkdir(parents=True, exist_ok=True)
    if target_thumb.exists():
        if target_thumb.is_dir():
            shutil.rmtree(target_thumb)
        else:
            target_thumb.unlink()
    shutil.move(str(source_thumb), str(target_thumb))


def _safe_folder_name(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    if not text or text in {".", ".."}:
        raise ValueError("フォルダー名を確認してください。")
    return text


def _unique_file_path(target_dir: Path, filename: str) -> Path:
    safe_name = re.sub(r'[\\/:*?"<>|]+', "_", str(filename or "image")).strip(" .")
    stem = Path(safe_name).stem or "image"
    suffix = Path(safe_name).suffix.lower()
    if suffix not in MEDIA_EXTENSIONS:
        raise ValueError("画像ファイルのみアップロードできます。")
    candidate = target_dir / f"{stem}{suffix}"
    counter = 1
    while candidate.exists():
        candidate = target_dir / f"{stem}_{counter:03d}{suffix}"
        counter += 1
    return candidate


def _next_number_for_folder(folder_value: str) -> dict:
    _ensure_upload_root()
    target_dir = _target_dir_for_folder(folder_value)
    max_number = 0
    matched = 0
    if target_dir.is_dir():
        for path in target_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            stem = path.stem
            match = re.search(r"(\d+)(?!.*\d)", stem)
            if not match:
                continue
            matched += 1
            max_number = max(max_number, int(match.group(1)))
    return {"nextNumber": max_number + 1, "maxNumber": max_number, "matched": matched}


def _next_numbered_file_path(target_dir: Path, suffix: str) -> Path:
    max_number = 0
    if target_dir.is_dir():
        for path in target_dir.iterdir():
            if not path.is_file():
                continue
            match = re.search(r"(\d+)(?!.*\d)", path.stem)
            if match:
                max_number = max(max_number, int(match.group(1)))
    width = 4 if max_number < 9999 else len(str(max_number + 1))
    while True:
        candidate = target_dir / f"{max_number + 1:0{width}d}{suffix}"
        if not candidate.exists():
            return candidate
        max_number += 1


def _image_request_headers(image_url: str) -> dict:
    host = urlparse(image_url).netloc.lower()
    referer = "https://x.com/" if "twimg.com" in host or "twitter.com" in host else "https://www.instagram.com/"
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": referer,
    }


def _download_instagram_image(image_url: str, target: Path) -> None:
    headers = _image_request_headers(image_url)
    target.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(image_url, headers=headers, stream=True, timeout=(5, 10)) as response:
        response.raise_for_status()
        with open(target, "wb") as fp:
            for chunk in response.iter_content(chunk_size=1024 * 128):
                if chunk:
                    fp.write(chunk)


def _generate_missing_thumbnails(force: bool = False, folder: str = "") -> dict:
    created = 0
    skipped = 0
    failed = 0
    errors = []
    with _thumb_lock:
        if force:
            _clear_thumbnail_cache(folder)
        for path in _iter_images(folder):
            try:
                if _generate_media_thumbnail(path):
                    created += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                if len(errors) < 5:
                    errors.append({"path": _relative_posix(path), "error": str(exc)})
    return {"created": created, "skipped": skipped, "failed": failed, "errors": errors, "force": force, "folder": folder}


def _write_thumb_job(job_id: str, **updates) -> dict:
    with _thumb_jobs_lock:
        job = dict(_thumb_jobs.get(job_id) or {})
        job.update(updates)
        job["updatedAt"] = time.time()
        _thumb_jobs[job_id] = job
        snapshot = dict(job)
    _ensure_upload_root()
    path = THUMB_JOB_ROOT / f"{job_id}.json"
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)
    return snapshot


def _read_thumb_job(job_id: str) -> dict:
    with _thumb_jobs_lock:
        job = dict(_thumb_jobs.get(job_id) or {})
    if job:
        return job
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(job_id or ""))
    path = THUMB_JOB_ROOT / f"{safe_job_id}.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _start_thumbnail_job(force: bool = False, folder: str = "") -> str:
    job_id = uuid.uuid4().hex
    _write_thumb_job(
        job_id,
        ok=True,
        status="pending",
        force=force,
        folder=folder,
        total=0,
        processed=0,
        created=0,
        skipped=0,
        failed=0,
        errors=[],
    )

    def worker() -> None:
        _write_thumb_job(job_id, status="waiting")
        created = 0
        skipped = 0
        failed = 0
        errors = []
        try:
            with _thumb_lock:
                _write_thumb_job(job_id, status="running")
                if force:
                    _clear_thumbnail_cache(folder)
                images = _iter_images(folder)
                total = len(images)
                _write_thumb_job(job_id, total=total)
                for index, path in enumerate(images, start=1):
                    try:
                        if _generate_media_thumbnail(path):
                            created += 1
                        else:
                            skipped += 1
                    except Exception as exc:
                        failed += 1
                        if len(errors) < 10:
                            errors.append({"path": _relative_posix(path), "error": str(exc)})
                    if index == total or index % 5 == 0:
                        _write_thumb_job(
                            job_id,
                            processed=index,
                            created=created,
                            skipped=skipped,
                            failed=failed,
                            errors=errors,
                        )
                _write_thumb_job(
                    job_id,
                    status="done",
                    processed=total,
                    total=total,
                    created=created,
                    skipped=skipped,
                    failed=failed,
                    errors=errors,
                )
        except Exception as exc:
            _write_thumb_job(job_id, status="error", error=str(exc))

    threading.Thread(target=worker, daemon=True).start()
    return job_id


def _start_thumbnail_worker_if_needed() -> None:
    global _thumb_worker_running
    if _thumb_worker_running:
        return

    def worker() -> None:
        global _thumb_worker_running
        try:
            _generate_missing_thumbnails()
        finally:
            _thumb_worker_running = False

    _thumb_worker_running = True
    threading.Thread(target=worker, daemon=True).start()


def _media_record(path: Path) -> dict:
    stat = path.stat()
    rel = _relative_posix(path)
    is_video = path.suffix.lower() in VIDEO_EXTENSIONS
    thumb_rel = _thumb_rel_for(path)
    has_fresh_thumb = _thumbnail_is_fresh(path)
    return {
        "name": path.name,
        "path": rel,
        "mediaType": "video" if is_video else "image",
        "folder": path.parent.relative_to(UPLOAD_ROOT).as_posix() if path.parent != UPLOAD_ROOT else "",
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
        "url": url_for("image_viewer.image_file", path=rel),
        "thumbUrl": url_for("image_viewer.thumbnail_file", path=thumb_rel) if has_fresh_thumb else None,
        "hasThumb": has_fresh_thumb,
    }


@image_viewer_bp.get("/")
@login_required
def index():
    _ensure_upload_root()
    return render_template("image_viewer.html")


@image_viewer_bp.get("/manifest.webmanifest")
def pwa_manifest():
    manifest = {
        "name": "MFU Image Viewer Desktop",
        "short_name": "Image Viewer",
        "description": "Windows XP style image and media viewer for MFU.",
        "start_url": url_for("image_viewer.index"),
        "scope": url_for("image_viewer.index"),
        "display": "standalone",
        "orientation": "any",
        "background_color": "#1f6fbd",
        "theme_color": "#0b63dd",
        "icons": [
            {
                "src": url_for("image_viewer.pwa_icon", size=192),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": url_for("image_viewer.pwa_icon", size=512),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
    return Response(json.dumps(manifest, ensure_ascii=False), mimetype="application/manifest+json")


@image_viewer_bp.get("/sw.js")
def pwa_service_worker():
    manifest_url = url_for("image_viewer.pwa_manifest")
    icon_192_url = url_for("image_viewer.pwa_icon", size=192)
    icon_512_url = url_for("image_viewer.pwa_icon", size=512)
    script = f"""
const CACHE_NAME = "mfu-image-viewer-pwa-v1";
const APP_ASSETS = [{json.dumps(manifest_url)}, {json.dumps(icon_192_url)}, {json.dumps(icon_512_url)}];

self.addEventListener("install", (event) => {{
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_ASSETS)));
  self.skipWaiting();
}});

self.addEventListener("activate", (event) => {{
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))))
  );
  self.clients.claim();
}});

self.addEventListener("fetch", (event) => {{
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (request.mode === "navigate") {{
    event.respondWith(
      fetch(request).catch(() => new Response(
        "<!doctype html><meta charset='utf-8'><title>Image Viewer</title><body style='font-family:sans-serif;padding:24px'>Image Viewer はオフラインでは起動できません。</body>",
        {{ headers: {{ "Content-Type": "text/html; charset=utf-8" }} }}
      ))
    );
    return;
  }}
  if (url.origin === location.origin && APP_ASSETS.includes(url.pathname)) {{
    event.respondWith(caches.match(request).then((cached) => cached || fetch(request)));
  }}
}});
"""
    return Response(script, mimetype="application/javascript")


@image_viewer_bp.get("/icon-<int:size>.png")
def pwa_icon(size: int):
    if size not in {192, 512}:
        abort(404)
    image = Image.new("RGBA", (size, size), (11, 99, 221, 255))
    inner = Image.new("RGBA", (size - size // 5, size - size // 5), (31, 111, 189, 255))
    image.alpha_composite(inner, (size // 10, size // 10))
    draw = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    from PIL import ImageDraw, ImageFont

    canvas = ImageDraw.Draw(draw)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(40, size // 4))
    except Exception:
        font = ImageFont.load_default()
    text = "IV"
    bbox = canvas.textbbox((0, 0), text, font=font)
    x = (size - (bbox[2] - bbox[0])) // 2
    y = (size - (bbox[3] - bbox[1])) // 2 - size // 28
    canvas.text((x + size // 80, y + size // 80), text, font=font, fill=(0, 33, 92, 150))
    canvas.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    image.alpha_composite(draw)
    output = BytesIO()
    image.save(output, "PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png", max_age=86400)


@image_viewer_bp.get("/api/images")
@login_required
def image_list():
    _ensure_upload_root()
    folders: set[str] = {""}
    images = []
    for path in sorted(UPLOAD_ROOT.rglob("*"), key=lambda p: p.as_posix().lower()):
        if _is_inside_hidden_thumb_dir(path):
            continue
        if path.is_dir():
            folders.add(_relative_posix(path))
            continue
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
            folders.add(path.parent.relative_to(UPLOAD_ROOT).as_posix() if path.parent != UPLOAD_ROOT else "")
            images.append(_media_record(path))
    return jsonify(
        {
            "ok": True,
            "root": str(UPLOAD_ROOT),
            "folders": sorted(folders, key=lambda v: (v.count("/"), v.lower())),
            "images": images,
            "thumbnailWorkerRunning": _thumb_worker_running,
        }
    )


@image_viewer_bp.post("/api/thumbnails")
@login_required
def create_thumbnails():
    data = request.get_json(silent=True) or {}
    folder = str(data.get("folder") or "")
    try:
        _target_dir_for_folder(folder)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc) or "フォルダーを確認してください。"}), 400
    job_id = _start_thumbnail_job(force=bool(data.get("force")), folder=folder)
    return jsonify({"ok": True, "status": "pending", "jobId": job_id})


@image_viewer_bp.get("/api/thumbnails/jobs/<job_id>")
@login_required
def thumbnail_job(job_id: str):
    job = _read_thumb_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "ジョブが見つかりません。"}), 404
    return jsonify({"ok": True, **job})


@image_viewer_bp.post("/api/folders")
@login_required
def create_folder():
    data = request.get_json(silent=True) or {}
    try:
        parent_dir = _target_dir_for_folder(data.get("parent") or "")
        folder_name = _safe_folder_name(data.get("name") or "")
        target = parent_dir / folder_name
        target.resolve().relative_to(UPLOAD_ROOT.resolve())
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc) or "フォルダーを作成できません。"}), 400
    if target.exists():
        return jsonify({"ok": False, "error": "同名フォルダーが既にあります。"}), 409
    target.mkdir(parents=False, exist_ok=False)
    return jsonify({"ok": True, "folder": target.relative_to(UPLOAD_ROOT).as_posix()})


@image_viewer_bp.post("/api/entries/rename")
@login_required
def rename_entry():
    _ensure_upload_root()
    data = request.get_json(silent=True) or {}
    entry_type = str(data.get("type") or "file")
    try:
        source = _target_path_for_rel(data.get("path") or "")
        new_name = _safe_folder_name(data.get("name") or "")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc) or "名前を確認してください。"}), 400
    if source == UPLOAD_ROOT:
        return jsonify({"ok": False, "error": "uploads直下のルートは名前変更できません。"}), 400
    if not source.exists():
        return jsonify({"ok": False, "error": "対象が見つかりません。"}), 404
    if entry_type == "folder" and not source.is_dir():
        return jsonify({"ok": False, "error": "フォルダーが見つかりません。"}), 404
    if entry_type != "folder" and not source.is_file():
        return jsonify({"ok": False, "error": "ファイルが見つかりません。"}), 404
    target = source.parent / new_name
    try:
        target.resolve().relative_to(UPLOAD_ROOT.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "名前を確認してください。"}), 400
    if target.exists() and target != source:
        return jsonify({"ok": False, "error": "同名のファイルまたはフォルダーが既にあります。"}), 409
    if source.name == new_name:
        return jsonify({"ok": True, "path": source.relative_to(UPLOAD_ROOT).as_posix()})
    is_dir = source.is_dir()
    try:
        source.rename(target)
        _move_thumb_for_path(source, target, is_dir)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc) or "名前変更に失敗しました。"}), 500
    return jsonify({"ok": True, "path": target.relative_to(UPLOAD_ROOT).as_posix(), "folder": target.relative_to(UPLOAD_ROOT).as_posix() if is_dir else target.parent.relative_to(UPLOAD_ROOT).as_posix()})


@image_viewer_bp.post("/api/entries/delete")
@login_required
def delete_entry():
    _ensure_upload_root()
    data = request.get_json(silent=True) or {}
    entry_type = str(data.get("type") or "file")
    try:
        target = _target_path_for_rel(data.get("path") or "")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc) or "対象を確認してください。"}), 400
    if target == UPLOAD_ROOT:
        return jsonify({"ok": False, "error": "uploads直下のルートは削除できません。"}), 400
    if not target.exists():
        return jsonify({"ok": False, "error": "対象が見つかりません。"}), 404
    if entry_type == "folder" and not target.is_dir():
        return jsonify({"ok": False, "error": "フォルダーが見つかりません。"}), 404
    if entry_type != "folder" and not target.is_file():
        return jsonify({"ok": False, "error": "ファイルが見つかりません。"}), 404
    try:
        _delete_thumb_for_path(target)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc) or "削除に失敗しました。"}), 500
    return jsonify({"ok": True})


@image_viewer_bp.post("/api/entries/move")
@login_required
def move_entry():
    _ensure_upload_root()
    data = request.get_json(silent=True) or {}
    entry_type = str(data.get("type") or "file")
    try:
        source = _target_path_for_rel(data.get("path") or "")
        destination_dir = _target_dir_for_folder(data.get("destination") or "")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc) or "移動先を確認してください。"}), 400
    if source == UPLOAD_ROOT:
        return jsonify({"ok": False, "error": "uploads直下のルートは移動できません。"}), 400
    if not source.exists():
        return jsonify({"ok": False, "error": "対象が見つかりません。"}), 404
    if not destination_dir.is_dir():
        return jsonify({"ok": False, "error": "移動先フォルダーが見つかりません。"}), 404
    if entry_type == "folder" and not source.is_dir():
        return jsonify({"ok": False, "error": "フォルダーが見つかりません。"}), 404
    if entry_type != "folder" and not source.is_file():
        return jsonify({"ok": False, "error": "ファイルが見つかりません。"}), 404
    is_dir = source.is_dir()
    if is_dir:
        try:
            destination_dir.resolve().relative_to(source.resolve())
            return jsonify({"ok": False, "error": "フォルダーを自分自身の配下へ移動できません。"}), 400
        except ValueError:
            pass
    target = destination_dir / source.name
    if target == source:
        return jsonify({"ok": True, "path": source.relative_to(UPLOAD_ROOT).as_posix()})
    if target.exists():
        return jsonify({"ok": False, "error": "移動先に同名のファイルまたはフォルダーが既にあります。"}), 409
    try:
        shutil.move(str(source), str(target))
        _move_thumb_for_path(source, target, is_dir)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc) or "移動に失敗しました。"}), 500
    return jsonify({"ok": True, "path": target.relative_to(UPLOAD_ROOT).as_posix(), "folder": target.relative_to(UPLOAD_ROOT).as_posix() if is_dir else target.parent.relative_to(UPLOAD_ROOT).as_posix()})


@image_viewer_bp.post("/api/upload")
@login_required
def upload_images():
    _ensure_upload_root()
    try:
        target_dir = _target_dir_for_folder(request.form.get("folder") or "")
    except ValueError:
        return jsonify({"ok": False, "error": "アップロード先フォルダーを確認してください。"}), 400
    target_dir.mkdir(parents=True, exist_ok=True)

    files = request.files.getlist("files")
    saved = []
    skipped = []
    errors = []
    for upload in files:
        if not upload or not upload.filename:
            continue
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in MEDIA_EXTENSIONS:
            skipped.append(upload.filename)
            continue
        try:
            target = _next_numbered_file_path(target_dir, suffix)
            upload.save(target)
            thumb_created = False
            if suffix in MEDIA_EXTENSIONS:
                try:
                    thumb_created = _generate_media_thumbnail(target)
                except Exception as exc:
                    errors.append({"name": upload.filename, "error": f"thumbnail: {exc}"})
            saved.append({"name": target.name, "path": target.relative_to(UPLOAD_ROOT).as_posix(), "thumbCreated": thumb_created})
        except Exception as exc:
            errors.append({"name": upload.filename, "error": str(exc)})
    return jsonify({"ok": True, "saved": saved, "skipped": skipped, "errors": errors})


@image_viewer_bp.post("/api/paste")
@login_required
def paste_images():
    _ensure_upload_root()
    try:
        target_dir = _target_dir_for_folder(request.form.get("folder") or "")
    except ValueError:
        return jsonify({"ok": False, "error": "保存先フォルダーを確認してください。"}), 400
    target_dir.mkdir(parents=True, exist_ok=True)

    files = request.files.getlist("files")
    saved = []
    skipped = []
    errors = []
    for upload in files:
        if not upload or not upload.filename:
            continue
        suffix = Path(upload.filename).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            content_type = (upload.mimetype or "").lower()
            if content_type == "image/jpeg":
                suffix = ".jpg"
            elif content_type == "image/webp":
                suffix = ".webp"
            elif content_type == "image/gif":
                suffix = ".gif"
            else:
                suffix = ".png"
        if suffix not in IMAGE_EXTENSIONS:
            skipped.append(upload.filename)
            continue
        try:
            target = _next_numbered_file_path(target_dir, suffix)
            upload.save(target)
            thumb_created = False
            try:
                thumb_created = _generate_media_thumbnail(target)
            except Exception as exc:
                errors.append({"name": upload.filename, "error": f"thumbnail: {exc}"})
            saved.append({"name": target.name, "path": target.relative_to(UPLOAD_ROOT).as_posix(), "thumbCreated": thumb_created})
        except Exception as exc:
            errors.append({"name": upload.filename, "error": str(exc)})
    if not saved and errors:
        return jsonify({"ok": False, "error": errors[0]["error"], "saved": saved, "skipped": skipped, "errors": errors}), 400
    return jsonify({"ok": True, "saved": saved, "skipped": skipped, "errors": errors})


@image_viewer_bp.post("/api/openai/illustration")
@login_required
def openai_illustration():
    data = request.get_json(silent=True) or {}
    try:
        job_id = _start_illustration_job(
            str(data.get("path") or ""),
            str(data.get("prompt") or ""),
            str(data.get("folder") or ""),
            str(data.get("quality") or "medium"),
            str(data.get("model") or ""),
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc) or "画像ファイルを確認してください。"}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc) or "生成ジョブを開始できませんでした。"}), 500
    return jsonify({"ok": True, "status": "pending", "jobId": job_id})


@image_viewer_bp.get("/api/openai/illustration/jobs/<job_id>")
@login_required
def openai_illustration_job(job_id: str):
    _cleanup_ai_jobs()
    job = _read_ai_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "生成ジョブが見つかりません。もう一度実行してください。"}), 404
    if job.get("status") == "error":
        return jsonify({"ok": False, "status": "error", "error": job.get("error") or "生成に失敗しました。"})
    result = dict(job)
    generated = dict(result.get("generated") or {})
    generated.pop("previewCache", None)
    if generated:
        generated["previewUrl"] = url_for("image_viewer.openai_illustration_preview", job_id=job_id)
        result["generated"] = generated
    return jsonify({"ok": True, **result})


@image_viewer_bp.get("/api/openai/illustration/jobs/<job_id>/preview")
@login_required
def openai_illustration_preview(job_id: str):
    job = _read_ai_job(job_id)
    generated = job.get("generated") or {}
    preview_cache = Path(str(generated.get("previewCache") or ""))
    if not preview_cache:
        abort(404)
    try:
        resolved = preview_cache.resolve()
        resolved.relative_to(AI_JOB_ROOT.resolve())
    except ValueError:
        abort(404)
    if not resolved.is_file():
        abort(404)
    return send_file(resolved, mimetype="image/png", max_age=300)


@image_viewer_bp.post("/api/openai/illustration/save")
@image_viewer_bp.post("/api/openai/illustration/jobs/<job_id>/save")
@login_required
def openai_illustration_save(job_id: str | None = None):
    _ensure_upload_root()
    data = request.get_json(silent=True) or {}
    job_id = str(job_id or data.get("jobId") or "")
    job = _read_ai_job(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"ok": False, "error": "保存できる生成結果がありません。"}), 404
    generated = job.get("generated") or {}
    preview_cache = Path(str(generated.get("previewCache") or ""))
    try:
        resolved = preview_cache.resolve()
        resolved.relative_to(AI_JOB_ROOT.resolve())
    except ValueError:
        return jsonify({"ok": False, "error": "生成結果を確認できません。"}), 400
    if not resolved.is_file():
        return jsonify({"ok": False, "error": "生成結果ファイルが見つかりません。"}), 404
    try:
        target_dir = _target_dir_for_folder(data.get("folder") or job.get("folder") or "")
    except ValueError:
        return jsonify({"ok": False, "error": "保存先フォルダーを確認してください。"}), 400
    target_dir.mkdir(parents=True, exist_ok=True)
    target = _next_numbered_file_path(target_dir, ".png")
    try:
        shutil.copyfile(resolved, target)
        try:
            _generate_media_thumbnail(target)
        except Exception:
            pass
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc) or "保存に失敗しました。"}), 500
    saved = {
        "name": target.name,
        "path": target.relative_to(UPLOAD_ROOT).as_posix(),
        "model": generated.get("model"),
        "quality": generated.get("quality"),
    }
    _set_ai_job(job_id, {"saved": saved})
    return jsonify({"ok": True, "saved": saved})


@image_viewer_bp.post("/api/instagram/fetch")
@login_required
def instagram_fetch():
    data = request.get_json(silent=True) or {}
    source, identifier = _extract_media_identifier(data.get("url") or data.get("shortcode") or "")
    if not source or not identifier:
        return jsonify({"ok": False, "error": "InstagramまたはXの投稿URLを確認してください。"}), 400
    job_id = _start_instagram_fetch_job(identifier, source)
    return jsonify({"ok": True, "status": "pending", "source": source, "shortcode": identifier, "jobId": job_id})


@image_viewer_bp.get("/api/instagram/jobs/<job_id>")
@login_required
def instagram_job(job_id: str):
    _cleanup_instagram_jobs()
    job = _read_instagram_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "取得ジョブが見つかりません。もう一度取得してください。"}), 404
    if job.get("status") == "error":
        return jsonify({"ok": False, "status": "error", "shortcode": job.get("shortcode"), "error": job.get("error")})
    images = []
    for item in job.get("images") or []:
        if isinstance(item, dict):
            next_item = dict(item)
            next_item.pop("previewCache", None)
            next_item["previewUrl"] = url_for("image_viewer.instagram_preview", job_id=job_id, index=int(next_item.get("index") or 0))
            images.append(next_item)
    return jsonify({
        "ok": True,
        "status": job.get("status"),
        "source": job.get("source") or "instagram",
        "shortcode": job.get("shortcode"),
        "total": job.get("total") or len(images),
        "processed": job.get("processed") or 0,
        "downloaded": job.get("downloaded") or 0,
        "failed": job.get("failed") or 0,
        "images": images,
    })


@image_viewer_bp.get("/api/instagram/jobs/<job_id>/preview/<int:index>")
@login_required
def instagram_preview(job_id: str, index: int):
    _cleanup_instagram_jobs()
    job = _read_instagram_job(job_id)
    if not job:
        return _empty_image_response()
    item = next((row for row in job.get("images") or [] if int(row.get("index") or 0) == index), None)
    if not item:
        return _empty_image_response()
    preview_cache = Path(str(item.get("previewCache") or ""))
    if preview_cache:
        try:
            resolved_cache = preview_cache.resolve()
            resolved_cache.relative_to(PREVIEW_ROOT.resolve())
        except ValueError:
            resolved_cache = None
        if resolved_cache and resolved_cache.is_file():
            return send_file(resolved_cache, max_age=3600)
    return _empty_image_response()


@image_viewer_bp.get("/api/instagram/next-number")
@login_required
def instagram_next_number():
    try:
        result = _next_number_for_folder(request.args.get("folder") or "")
    except ValueError:
        return jsonify({"ok": False, "error": "保存先フォルダーは画像ビュアーのアップロード配下のみ指定できます。"}), 400
    return jsonify({"ok": True, **result})


@image_viewer_bp.post("/api/instagram/save")
@login_required
def instagram_save():
    _ensure_upload_root()
    data = request.get_json(silent=True) or {}
    shortcode = _extract_shortcode(data.get("shortcode") or "")
    images = data.get("images") or []
    job_id = str(data.get("jobId") or "")
    selected = data.get("selected") or []
    if not shortcode or not isinstance(images, list):
        return jsonify({"ok": False, "error": "保存データが不正です。"}), 400
    selected_indexes = {int(v) for v in selected if str(v).isdigit()}
    if not selected_indexes:
        return jsonify({"ok": False, "error": "保存する画像を選択してください。"}), 400

    target_dir = None
    try:
        target_dir = _target_dir_for_folder(data.get("folder") or "")
    except ValueError:
        return jsonify({"ok": False, "error": "保存先は画像ビュアーのアップロード配下のみ指定できます。"}), 400

    start_number = max(1, int(data.get("startNumber") or 1))
    digits = min(6, max(1, int(data.get("digits") or 3)))

    saved = []
    errors = []
    job_images = []
    if job_id:
        job = _read_instagram_job(job_id)
        if job.get("shortcode") == shortcode:
            job_images = job.get("images") or []
    source_images = job_images if job_images else images
    by_index = {int(item.get("index") or 0): item for item in source_images if isinstance(item, dict)}
    for offset, item_index in enumerate(sorted(selected_indexes)):
        item = by_index.get(item_index)
        if not item:
            continue
        image_url = str(item.get("url") or "")
        suffix = _guess_image_suffix(image_url)
        filename = f"{start_number + offset:0{digits}d}{suffix}"
        target = target_dir / filename
        try:
            preview_cache = Path(str(item.get("previewCache") or ""))
            copied = False
            if preview_cache:
                try:
                    resolved_cache = preview_cache.resolve()
                    resolved_cache.relative_to(PREVIEW_ROOT.resolve())
                except ValueError:
                    resolved_cache = None
                if resolved_cache and resolved_cache.is_file():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(resolved_cache, target)
                    copied = True
            if not copied:
                _download_instagram_image(image_url, target)
            try:
                _generate_media_thumbnail(target)
            except Exception:
                pass
            saved.append({"name": filename, "path": target.relative_to(UPLOAD_ROOT).as_posix()})
        except Exception as exc:
            errors.append({"index": item_index, "error": str(exc)})
    if errors and not saved:
        return jsonify({"ok": False, "error": errors[0]["error"], "errors": errors})
    return jsonify({"ok": True, "saved": saved, "errors": errors})


@image_viewer_bp.post("/api/video/fetch")
@login_required
def video_fetch():
    data = request.get_json(silent=True) or {}
    source, identifier = _extract_media_identifier(data.get("url") or "")
    if not source or not identifier:
        return jsonify({"ok": False, "error": "InstagramまたはXの投稿URLを確認してください。"}), 400
    job_id = _start_video_fetch_job(identifier, source)
    return jsonify({"ok": True, "status": "pending", "source": source, "identifier": identifier, "jobId": job_id})


@image_viewer_bp.get("/api/video/jobs/<job_id>")
@login_required
def video_job(job_id: str):
    _cleanup_video_jobs()
    job = _read_video_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "取得ジョブが見つかりません。もう一度取得してください。"}), 404
    if job.get("status") == "error":
        return jsonify({"ok": False, "status": "error", "identifier": job.get("identifier"), "error": job.get("error")})
    return jsonify(
        {
            "ok": True,
            "status": job.get("status"),
            "source": job.get("source") or "instagram",
            "identifier": job.get("identifier"),
            "videos": job.get("videos") or [],
        }
    )


@image_viewer_bp.post("/api/video/save")
@login_required
def video_save():
    _ensure_upload_root()
    data = request.get_json(silent=True) or {}
    videos = data.get("videos") or []
    job_id = str(data.get("jobId") or "")
    selected = data.get("selected") or []
    selected_indexes = {int(v) for v in selected if str(v).isdigit()}
    if not selected_indexes:
        return jsonify({"ok": False, "error": "保存する動画を選択してください。"}), 400
    try:
        target_dir = _target_dir_for_folder(data.get("folder") or "")
    except ValueError:
        return jsonify({"ok": False, "error": "保存先は画像ビューアーのアップロード配下のみ指定できます。"}), 400
    target_dir.mkdir(parents=True, exist_ok=True)

    job_videos = []
    if job_id:
        job = _read_video_job(job_id)
        job_videos = job.get("videos") or []
    source_videos = job_videos if job_videos else videos
    by_index = {int(item.get("index") or 0): item for item in source_videos if isinstance(item, dict)}
    saved = []
    errors = []
    for item_index in sorted(selected_indexes):
        item = by_index.get(item_index)
        if not item:
            continue
        video_url = str(item.get("url") or "")
        suffix = _guess_video_suffix(video_url)
        filename = item.get("filename") or f"video_{item_index:03d}{suffix}"
        if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
            filename = f"{Path(filename).stem or 'video'}{suffix}"
        try:
            target = _unique_file_path(target_dir, filename)
            _download_instagram_image(video_url, target)
            try:
                _generate_media_thumbnail(target)
            except Exception:
                pass
            saved.append({"name": target.name, "path": target.relative_to(UPLOAD_ROOT).as_posix()})
        except Exception as exc:
            errors.append({"index": item_index, "error": str(exc)})
    if errors and not saved:
        return jsonify({"ok": False, "error": errors[0]["error"], "errors": errors})
    return jsonify({"ok": True, "saved": saved, "errors": errors})


@image_viewer_bp.get("/files/<path:path>")
@login_required
def image_file(path: str):
    _ensure_upload_root()
    safe_path = safe_join(str(UPLOAD_ROOT), path)
    if not safe_path:
        abort(404)
    resolved = Path(safe_path).resolve()
    try:
        resolved.relative_to(UPLOAD_ROOT.resolve())
    except ValueError:
        abort(404)
    if not resolved.is_file() or resolved.suffix.lower() not in MEDIA_EXTENSIONS:
        abort(404)
    return send_from_directory(str(resolved.parent), resolved.name, as_attachment=False, max_age=3600)


@image_viewer_bp.get("/thumbs/<path:path>")
@login_required
def thumbnail_file(path: str):
    _ensure_upload_root()
    safe_path = safe_join(str(THUMB_ROOT), path)
    if not safe_path:
        abort(404)
    resolved = Path(safe_path).resolve()
    try:
        resolved.relative_to(THUMB_ROOT.resolve())
    except ValueError:
        abort(404)
    if not resolved.is_file() or resolved.suffix.lower() != ".webp":
        return _empty_image_response()
    return send_file(resolved, mimetype="image/webp", max_age=86400)
