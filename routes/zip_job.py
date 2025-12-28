# app/routes/zip_job.py
"""
MFU: ZIP pre-generation workflow (ticket-based)
- POST /api/zip-ticket         : create job (returns ticket UUID)
- GET  /api/zip-status/<ticket>: check progress / ready / error
- GET  /api/zip-download/<t>   : download <UUID>.zip (native browser download)

Key points
- ZIP filename (real & download): <ticket>.zip (UUID)
- Client sends subpaths like "<uuid>/original/foo.jpg"
- Server resolves them safely under configured storage roots
- Prevents empty ZIP (22B): rejects if no valid files resolved (HTTP 400)
- Progress metadata: /mnt/mfu/tmp/mfu-progress/<ticket>.json
- ZIP output RAM: /dev/shm/mfu/<ticket>.zip (change if you want)

Configure storage roots (any of these):
- app.config["UPLOADS_DIR"] / ["FILES_BASE"] / ["STORAGE_ROOT"]
- env MFU_ZIP_ROOTS="/mnt/mfu/uploads:/mnt/mfu"
- built-in guesses: /mnt/mfu/uploads, /mnt/mfu/upload, /mnt/mfu
"""

from __future__ import annotations

import os
import json
import uuid
import zipfile
from pathlib import Path
from typing import List, Optional

from flask import (
    Blueprint, request, jsonify, abort, send_file, make_response, current_app
)
from concurrent.futures import ThreadPoolExecutor
from werkzeug.utils import secure_filename

bp = Blueprint("zip_job", __name__)

# === Paths (adjust if needed) =================================================
ZIP_DIR  = Path("/dev/shm/mfu")                  # where .zip files are created
PROG_DIR = Path("/mnt/mfu/tmp/mfu-progress")     # where progress json is stored
ZIP_DIR.mkdir(parents=True, exist_ok=True)
PROG_DIR.mkdir(parents=True, exist_ok=True)

# === Concurrency ============================================================== 
EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("ZIP_WORKERS", "2")))

# === File types that should NOT be deflated (already compressed) =============
ALREADY_COMPRESSED = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
    ".mp4", ".mov", ".m4v", ".webm", ".mkv",
    ".mp3", ".aac", ".ogg", ".flac",
    ".pdf", ".zip", ".7z", ".rar"
}

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def _read_payload() -> tuple[List[str], bool]:
    """
    Accept JSON or form. We ignore any 'label' (kept only for backward compat).
    Returns: (paths, preserve_dirs_bool)
    """
    if request.is_json:
        d = request.get_json(silent=True) or {}
        paths = d.get("paths", [])
        preserve = bool(d.get("preserve_dirs"))
        return paths, preserve
    # form fallback
    return request.form.getlist("paths"), request.form.get("preserve_dirs") in ("1", "true", "on", "yes")


def _base_candidates() -> List[Path]:
    """
    Build list of storage roots:
      1) Flask config: UPLOADS_DIR / FILES_BASE / STORAGE_ROOT
      2) env MFU_ZIP_ROOTS=":/sep"
      3) built-in guesses
    """
    roots: List[Path] = []
    cfg = getattr(current_app, "config", {}) or {}

    for key in ("UPLOADS_DIR", "FILES_BASE", "STORAGE_ROOT"):
        v = cfg.get(key)
        if v:
            p = Path(v).resolve()
            if p.exists():
                roots.append(p)

    env = os.getenv("MFU_ZIP_ROOTS", "")
    if env:
        for part in env.split(":"):
            part = part.strip()
            if not part:
                continue
            p = Path(part).resolve()
            if p.exists():
                roots.append(p)

    for guess in ("/mnt/mfu/uploads", "/mnt/mfu/upload", "/mnt/mfu"):
        p = Path(guess).resolve()
        if p.exists():
            roots.append(p)

    # unique, keep order
    out, seen = [], set()
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _resolve_one(rel: str) -> Optional[Path]:
    """
    Resolve a client-provided *relative* subpath like "uuid/original/file.jpg"
    to an absolute file path under any storage root. Prevent path traversal.
    """
    if not rel or rel.endswith("/"):
        return None

    # sanitize parts (strip ., .., empty). Keep unicode names; only last part soft-sanitized for safety.
    parts = [p for p in Path(rel).parts if p not in ("", ".", "..")]
    if not parts:
        return None

    # Keep a safer basename but preserve original if secure_filename() empties
    safe_tail = secure_filename(parts[-1]) or parts[-1]
    parts[-1] = safe_tail

    for base in _base_candidates():
        cand = (base.joinpath(*parts)).resolve()
        # must be within base (no traversal)
        try:
            _ = cand.relative_to(base)
        except Exception:
            continue
        if cand.exists() and cand.is_file():
            return cand

    return None


def _resolve_paths(paths: List[str]) -> List[Path]:
    """
    Resolve mixed list (absolute or relative) into absolute file paths.
    Deduplicates. Skips non-existent.
    """
    out, seen = [], set()
    for item in paths:
        p: Optional[Path] = None
        try:
            candidate = Path(item)
            if candidate.is_absolute() and candidate.exists() and candidate.is_file():
                p = candidate.resolve()
            else:
                p = _resolve_one(item)
        except Exception:
            # fallback to relative resolution
            p = _resolve_one(item)

        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _write_progress(ticket: str, **kw) -> None:
    meta = PROG_DIR / f"{ticket}.json"
    meta.write_text(json.dumps({"ticket": ticket, **kw}, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------------------
# ZIP build job
# ------------------------------------------------------------------------------

def _common_root(paths: List[Path]) -> Optional[Path]:
    if not paths:
        return None
    try:
        return Path(os.path.commonpath([str(p.parent) for p in paths]))
    except Exception:
        return None


def _build_zip_job(ticket: str, resolved_abs_paths: List[str], preserve_dirs: bool) -> None:
    """
    Build the ZIP at ZIP_DIR/<ticket>.zip using already-resolved absolute paths.
    Progress is written to PROG_DIR/<ticket>.json.
    """
    try:
        _write_progress(ticket, state="running", progress=0)

        files = [Path(p) for p in resolved_abs_paths if Path(p).exists() and Path(p).is_file()]
        total = len(files)
        if total == 0:
            _write_progress(ticket, state="error", progress=0, error="no files")
            return

        root_common = _common_root(files) if preserve_dirs else None

        out_zip = ZIP_DIR / f"{ticket}.zip"
        try:
            out_zip.unlink(missing_ok=True)
        except Exception:
            pass

        done = 0
        with zipfile.ZipFile(out_zip, "w", allowZip64=True) as zf:
            for p in files:
                # archive name: either relative to common root, or just the basename
                if preserve_dirs and root_common:
                    try:
                        arc = str(p.relative_to(root_common))
                    except Exception:
                        arc = p.name
                else:
                    arc = p.name

                compress_type = zipfile.ZIP_STORED if p.suffix.lower() in ALREADY_COMPRESSED else zipfile.ZIP_DEFLATED
                zf.write(p, arcname=arc, compress_type=compress_type)

                done += 1
                _write_progress(ticket, state="running", progress=int(done * 100 / total))

        size = out_zip.stat().st_size
        _write_progress(
            ticket,
            state="ready",
            progress=100,
            size=size,
            download_url=f"/api/zip-download/{ticket}",
            download_name=f"{ticket}.zip",
        )

    except Exception as e:
        _write_progress(ticket, state="error", progress=0, error=str(e))


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

@bp.route("/api/zip-ticket", methods=["POST"])
def api_zip_ticket():
    """
    Create a ticket and start a background job that builds <ticket>.zip.
    Reject when no valid files could be resolved (prevents 22B empty ZIP).
    """
    paths, preserve = _read_payload()
    if not paths:
        return jsonify({"error": "no paths"}), 400

    # Resolve once here to validate and to freeze the exact file list
    resolved = _resolve_paths(paths)
    if not resolved:
        return jsonify({"error": "no valid files", "note": "paths not resolvable under storage roots"}), 400

    ticket = uuid.uuid4().hex
    _write_progress(ticket, state="queued", progress=0)

    # Pass the absolute list to the job (strings)
    EXECUTOR.submit(_build_zip_job, ticket, [str(p) for p in resolved], preserve)

    return jsonify({"ticket": ticket})


@bp.route("/api/zip-status/<ticket>", methods=["GET"])
def api_zip_status(ticket: str):
    meta = PROG_DIR / f"{ticket}.json"
    if not meta.exists():
        return jsonify({"error": "not_found"}), 404
    return current_app.response_class(meta.read_text("utf-8"), mimetype="application/json")


@bp.route("/api/zip-download/<ticket>", methods=["GET"])
def api_zip_download(ticket: str):
    meta = PROG_DIR / f"{ticket}.json"
    if not meta.exists():
        abort(404)

    info = json.loads(meta.read_text("utf-8"))
    if info.get("state") != "ready":
        return jsonify({"error": "not_ready"}), 409

    out_zip = ZIP_DIR / f"{ticket}.zip"
    if not out_zip.exists():
        return jsonify({"error": "file_missing"}), 410

    download_name = f"{ticket}.zip"  # always UUID.zip

    # --- Simple: send via Flask ------------------------------------------------
    resp = make_response(send_file(out_zip, as_attachment=True, download_name=download_name))
    resp.headers["Content-Type"] = "application/zip"
    resp.headers["Content-Length"] = str(out_zip.stat().st_size)
    resp.headers["Accept-Ranges"] = "bytes"
    return resp

    # --- Recommended (optional): offload to Nginx via X-Accel-Redirect --------
    # resp = make_response("")
    # resp.headers["Content-Type"] = "application/zip"
    # resp.headers["Content-Disposition"] = f"attachment; filename={download_name}"
    # resp.headers["Content-Length"] = str(out_zip.stat().st_size)
    # resp.headers["Accept-Ranges"] = "bytes"
    # resp.headers["X-Accel-Redirect"] = f"/_protected/zip/{out_zip.name}"
    # return resp
