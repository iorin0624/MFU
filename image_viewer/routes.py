from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from functools import wraps
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
THUMB_SIZE = (360, 360)
THUMB_QUALITY = 82
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
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
_INSTAGRAM_JOB_TTL_SECONDS = 15 * 60


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
            preview_path = PREVIEW_ROOT / job_id / f"{idx:03d}{suffix}"
            try:
                _download_instagram_image(image_url, preview_path)
                item["previewCache"] = str(preview_path)
                item["previewReady"] = True
            except Exception as exc:
                item["previewReady"] = False
                item["previewError"] = str(exc)
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
            "error": "",
        },
    )

    def worker() -> None:
        try:
            items = _media_image_items(source, shortcode)
            if not items:
                raise RuntimeError("画像を取得できませんでした。")
            payload = _instagram_image_payload(shortcode, items, job_id, source)
            _set_instagram_job(job_id, {"status": "done", "images": payload})
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


def _safe_relative_folder(value: str) -> Path:
    text = str(value or "").replace("\\", "/").strip().strip("/")
    parts = [part for part in text.split("/") if part and part not in {".", ".."}]
    return Path(*parts) if parts else Path()


def _target_dir_for_folder(folder_value: str) -> Path:
    target_dir = UPLOAD_ROOT / _safe_relative_folder(folder_value or "")
    target_dir.resolve().relative_to(UPLOAD_ROOT.resolve())
    return target_dir


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
    with requests.get(image_url, headers=headers, stream=True, timeout=45) as response:
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
            target = _unique_file_path(target_dir, upload.filename)
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

    image_url = str(item.get("url") or "")
    headers = _image_request_headers(image_url)
    try:
        upstream = requests.get(image_url, headers=headers, stream=True, timeout=45)
        upstream.raise_for_status()
    except Exception:
        return _empty_image_response()

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 128):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        mimetype=upstream.headers.get("content-type") or "image/jpeg",
        headers={"Cache-Control": "private, max-age=300"},
    )


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
