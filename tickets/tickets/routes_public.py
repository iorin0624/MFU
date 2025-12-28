# -*- coding: utf-8 -*-
"""
利用者向け（公開）ルート:
  - GET  /t/                            … 短縮コード入力ページ
  - POST /t/                            … 短縮コード照合→/t/<uuid>へ302
  - GET  /s/<short_code>                … 直接短縮コードから /t/<uuid> へ302
  - GET  /t/<uuid>                      … ダウンロードUI（HTML）
  - GET  /tickets/public/<uuid>         … 同上(互換)
  - GET  /tickets/api/files/<uuid>      … 画像一覧API（thumb_url / view_url / orig_name）
  - GET  /tickets/api/status/<uuid>     … 残り回数/クールダウン状況API
  - POST /tickets/api/zip/<uuid>        … ZIP生成API（選択DL）
  - GET  /tickets/dl/<token>.zip        … 生成ZIP配信
  - GET  /tickets/thumb/<uuid>/<name>   … サムネイル配信（.webp）
  - GET  /tickets/preview/<uuid>/<name> … プレビュー配信（縮小＋透かし）
"""
import os
import io
import re
import json
import math
from uuid import uuid4
from datetime import datetime
from flask import (
    request, render_template, jsonify, abort, send_file, current_app, redirect, url_for
)

from app.utils.db import get_db
from app.utils import zip_stream
from . import tickets_bp

# Pillow（無ければ原寸でフォールバック）
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except Exception:
    PIL_OK = False

TICKETS_PHOTO_ROOT = "/mnt/mfu/tickets_photo"
HEX8_RE = re.compile(r"^[0-9a-f]{8}$")

# ---------- ヘルパ ----------
def _now():
    return datetime.now()

def _tmp_root():
    return current_app.config.get("TMP_ROOT", "/mnt/mfu/tmp")

def _meta_path(token: str) -> str:
    return os.path.join(_tmp_root(), f"{token}.json")

def _batch_guard(b: dict) -> str | None:
    today = _now().date()
    view_start = b.get("view_start")
    expires_at = b.get("expires_at")
    if view_start and today < view_start:
        return "このページはまだ閲覧開始前です。"
    if expires_at and _now() > expires_at:
        return "有効期限を過ぎています。"
    return None

def _load_ticket_context(uuid: str):
    """テンプレ/各API共通のコンテキストを取得"""
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("""
      SELECT t.id AS ticket_id, t.batch_id, t.uuid,
             b.event_name, b.view_start, b.expires_at,
             b.max_downloads, b.cooldown_secs,
             b.zip_limit_files, b.zip_limit_bytes,
             b.public_usage_message
      FROM tickets t
      JOIN ticket_batches b ON b.id = t.batch_id
      WHERE t.uuid=%s
    """, (uuid,))
    row = cur.fetchone()
    db.close()
    return row

def _thumb_url(uuid: str, webp_name: str) -> str:
    return f"/tickets/thumb/{uuid}/{webp_name}"

def _preview_url(uuid: str, filename: str) -> str:
    return f"/tickets/preview/{uuid}/{filename}"

def _orig_path(batch_id: int, filename: str) -> str:
    return os.path.join(TICKETS_PHOTO_ROOT, str(batch_id), "original", filename)

def _thumb_path(batch_id: int, webp_name: str) -> str:
    return os.path.join(TICKETS_PHOTO_ROOT, str(batch_id), "thumbnail", webp_name)

def _usage_info(ticket_id: int):
    """
    使用状況を取得（tickets.* を優先）
    returns: (total_used:int, last_dt:datetime|None, from_counter:bool)
    """
    db = get_db()
    cur = db.cursor(dictionary=True)
    try:
        cur.execute("SELECT download_count, last_download_at FROM tickets WHERE id=%s", (ticket_id,))
        row = cur.fetchone()
        db.close()
        used = int((row or {}).get("download_count") or 0)
        last_dt = (row or {}).get("last_download_at")
        return used, last_dt, True
    except Exception:
        try:
            cur.execute("SELECT COUNT(*) AS c FROM ticket_downloads WHERE ticket_id=%s", (ticket_id,))
            used = int((cur.fetchone() or {}).get("c", 0))
            cur.execute("""
                SELECT created_at FROM ticket_downloads
                WHERE ticket_id=%s ORDER BY id DESC LIMIT 1
            """, (ticket_id,))
            last = cur.fetchone()
            last_dt = last["created_at"] if last else None
        finally:
            db.close()
        return used, last_dt, False

def _record_download(ticket_id: int, zip_token: str, size_bytes: int, files_count: int = 0):
    """ダウンロードを記録。tickets のカウンタ前進も試行"""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO ticket_downloads (ticket_id, token, size_bytes, total_bytes, file_count)
            VALUES (%s, %s, %s, %s, %s)
        """, (ticket_id, zip_token, size_bytes, size_bytes, files_count))

        try:
            cur.execute("""
                UPDATE tickets
                   SET download_count = COALESCE(download_count,0) + 1,
                       last_download_at = NOW()
                 WHERE id = %s
            """, (ticket_id,))
        except Exception:
            pass

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# ---------- 入口: 短縮コード入力/解決 ----------
@tickets_bp.route("/t/", methods=["GET"])
def ticket_short_input():
    return render_template("ticket_short_input.html")

@tickets_bp.route("/t/", methods=["POST"])
def ticket_short_resolve():
    batch_id_raw = (request.form.get("batch_id") or "").strip()
    token8 = (request.form.get("token8") or "").strip().lower()

    try:
        batch_id = int(batch_id_raw)
        if batch_id <= 0:
            raise ValueError
    except Exception:
        return render_template(
            "ticket_short_input.html",
            error="バッチIDは正の整数で入力してください。",
            last_batch=batch_id_raw, last_token=token8
        ), 400

    if not HEX8_RE.fullmatch(token8):
        return render_template(
            "ticket_short_input.html",
            error="トークンは8桁の16進（小文字）で入力してください。",
            last_batch=batch_id_raw, last_token=token8
        ), 400

    short_code = f"{batch_id}-{token8}"

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("""
        SELECT uuid FROM tickets
         WHERE batch_id=%s AND short_code=%s
        LIMIT 1
    """, (batch_id, short_code))
    row = cur.fetchone()
    db.close()

    if not row:
        return render_template(
            "ticket_short_input.html",
            error="該当するチケットが見つかりません。",
            last_batch=batch_id_raw, last_token=token8
        ), 404

    return redirect(url_for("tickets.ticket_public", uuid=row["uuid"]), code=302)

@tickets_bp.route("/s/<short_code>")
def ticket_public_short(short_code: str):
    try:
        parts = short_code.split("-", 1)
        if len(parts) != 2:
            raise ValueError
        batch_id = int(parts[0])
        token8 = parts[1]
        if batch_id <= 0 or not HEX8_RE.fullmatch(token8):
            raise ValueError
    except Exception:
        abort(404)

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("""
        SELECT uuid FROM tickets
         WHERE batch_id=%s AND short_code=%s
        LIMIT 1
    """, (batch_id, short_code))
    row = cur.fetchone()
    db.close()

    if not row:
        abort(404)

    return redirect(url_for("tickets.ticket_public", uuid=row["uuid"]), code=302)

# ---------- 画面（従来UUID入口） ----------
@tickets_bp.route("/t/<uuid>")
@tickets_bp.route("/tickets/public/<uuid>")
def ticket_public(uuid):
    ctx = _load_ticket_context(uuid)
    if not ctx:
        abort(404)
    msg = _batch_guard(ctx)
    return render_template("ticket_download.html", ctx=ctx, guard_msg=msg)

# ---------- API: ステータス ----------
@tickets_bp.route("/tickets/api/status/<uuid>")
def api_status(uuid):
    ctx = _load_ticket_context(uuid)
    if not ctx:
        return jsonify({"ok": False, "error": "not_found"}), 404

    total_used, last_dt, _from_counter = _usage_info(ctx["ticket_id"])
    total_limit = int(ctx.get("max_downloads") or 0)   # 0=無制限
    cooldown    = int(ctx.get("cooldown_secs") or 0)

    remain_total = max(0, total_limit - total_used) if total_limit > 0 else None

    cooldown_remain = 0
    if last_dt:
        delta = (_now() - last_dt).total_seconds()
        cooldown_remain = max(0, int(cooldown - delta))

    return jsonify({
        "ok": True,
        "limits": {
            "total_limit": total_limit,
            "daily_limit": 0,
            "cooldown_secs": cooldown
        },
        "usage": {
            "total_used": total_used,
            "daily_used": 0,
            "remain_total": remain_total,
            "remain_daily": None,
            "cooldown_remain_secs": cooldown_remain
        }
    })

# ---------- API: 画像一覧 ----------
@tickets_bp.route("/tickets/api/files/<uuid>")
def api_files(uuid):
    ctx = _load_ticket_context(uuid)
    if not ctx:
        return jsonify({"ok": False, "error": "not_found"}), 404

    msg = _batch_guard(ctx)
    if msg:
        return jsonify({"ok": False, "error": "forbidden", "message": msg}), 403

    page  = max(1, int(request.args.get("page", 1)))
    size  = min(100, max(10, int(request.args.get("size", 50))))  # 10-100
    offset = (page - 1) * size

    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("""
      SELECT id, filename, size_bytes, shot_at
      FROM ticket_files
      WHERE batch_id=%s
      ORDER BY filename ASC
      LIMIT %s OFFSET %s
    """, (ctx["batch_id"], size, offset))
    rows = cur.fetchall()
    db.close()

    items = []
    for r in rows:
        webp = os.path.splitext(r["filename"])[0] + ".webp"
        items.append({
            "id": r["id"],
            "filename": r["filename"],
            "shot_at": (r["shot_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("shot_at") else None),
            "size_bytes": r["size_bytes"],
            "thumb_url": _thumb_url(uuid, webp),
            "view_url":  _preview_url(uuid, r["filename"]),
            "orig_name": r["filename"],
        })

    return jsonify({"ok": True, "items": items, "page": page, "size": size})

# ---------- API: ZIP生成 ----------
@tickets_bp.route("/tickets/api/zip/<uuid>", methods=["POST"])
def api_zip(uuid):
    ctx = _load_ticket_context(uuid)
    if not ctx:
        return jsonify({"ok": False, "error": "not_found"}), 404

    msg = _batch_guard(ctx)
    if msg:
        return jsonify({"ok": False, "error": "forbidden", "message": msg}), 403

    body = request.get_json(silent=True) or {}
    files = body.get("files") or []

    if not files:
        return jsonify({"ok": False, "error": "no_files"}), 400

    # 件数上限（0 or None の時は無制限扱い）
    limit_files = int(ctx.get("zip_limit_files") or 0)
    if limit_files > 0 and len(files) > limit_files:
        return jsonify({"ok": False, "error": "too_many", "limit": limit_files}), 400

    # 全体上限とCD
    total_used, last_dt, _ = _usage_info(ctx["ticket_id"])
    max_total = int(ctx.get("max_downloads") or 0)  # 0=無制限
    cooldown  = int(ctx.get("cooldown_secs") or 0)

    if max_total > 0 and total_used >= max_total:
        return jsonify({"ok": False, "error": "max_reached"}), 429
    if last_dt and (_now() - last_dt).total_seconds() < cooldown:
        remain = int(cooldown - (_now() - last_dt).total_seconds())
        return jsonify({"ok": False, "error": "cooldown", "remain": remain}), 429

    # 容量チェック（0 or None は無制限）
    limit_bytes = int(ctx.get("zip_limit_bytes") or 0)
    relpaths = [f"tickets/{ctx['batch_id']}/original/{name}" for name in files]
    abs_paths, size_sum = [], 0
    for rel in relpaths:
        full = zip_stream.resolve_relpath(rel)
        if not full or not os.path.isfile(full):
            return jsonify({"ok": False, "error": "not_found_file", "path": rel}), 400
        s = os.path.getsize(full)
        size_sum += s
        if limit_bytes > 0 and size_sum > limit_bytes:
            return jsonify({"ok": False, "error": "bytes_exceeded", "limit": limit_bytes}), 400
        abs_paths.append(full)

    token = uuid4().hex
    zip_path = zip_stream.make_zip_file(abs_paths, token)
    if not zip_path or not os.path.isfile(zip_path):
        return jsonify({"ok": False, "error": "zip_failed"}), 500

    meta = {
        "ticket_id": ctx["ticket_id"],
        "size_bytes": size_sum,
        "files_count": len(files),
        "created_at": _now().isoformat()
    }
    try:
        with open(_meta_path(token), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "zip_url": f"/tickets/dl/{token}.zip",
        "expire_minutes": 10,
        "size_bytes": size_sum
    })

# ---------- 生成ZIP配信 ----------
@tickets_bp.route("/tickets/dl/<token>.zip")
def ticket_zip_download(token):
    zip_path = os.path.join(_tmp_root(), f"{token}.zip")
    if not os.path.isfile(zip_path):
        abort(404)

    meta_path = _meta_path(token)
    meta = None
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = None

    if meta:
        ticket_id  = int(meta.get("ticket_id"))
        size_bytes = int(meta.get("size_bytes", 0))
        files_count = int(meta.get("files_count", 0))

        # レース対策の最終チェック
        db = get_db()
        cur = db.cursor(dictionary=True)
        cur.execute("""
          SELECT b.max_downloads, b.cooldown_secs
          FROM tickets t JOIN ticket_batches b ON b.id=t.batch_id
          WHERE t.id=%s
        """, (ticket_id,))
        lim = cur.fetchone()
        db.close()

        if lim:
            total_used, last_dt, _ = _usage_info(ticket_id)
            max_total = int(lim.get("max_downloads") or 0)
            cooldown  = int(lim.get("cooldown_secs") or 0)

            if max_total > 0 and total_used >= max_total:
                return ("ダウンロード上限（総回数）に達しました。", 429)
            if last_dt and (_now() - last_dt).total_seconds() < cooldown:
                return ("クールダウン中です。しばらく待ってからお試しください。", 429)

        # 記録
        _record_download(ticket_id, token, size_bytes, files_count)

    return send_file(zip_path, mimetype="application/zip", as_attachment=True, download_name="photos.zip")

# ---------- サムネ（.webp） ----------
@tickets_bp.route("/tickets/thumb/<uuid>/<path:filename>")
def ticket_thumb_public(uuid, filename):
    ctx = _load_ticket_context(uuid)
    if not ctx:
        abort(404)
    msg = _batch_guard(ctx)
    if msg:
        abort(403)
    path = _thumb_path(ctx["batch_id"], filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/webp")

# ---------- プレビュー（縮小＋透かし） ----------
@tickets_bp.route("/tickets/preview/<uuid>/<path:filename>")
def ticket_preview_public(uuid, filename):
    ctx = _load_ticket_context(uuid)
    if not ctx:
        abort(404)
    msg = _batch_guard(ctx)
    if msg:
        abort(403)

    orig = _orig_path(ctx["batch_id"], filename)
    if not os.path.isfile(orig):
        abort(404)

    max_side   = int(float(request.args.get("w", 2048)))
    wm_text    = request.args.get("wm", "SAMPLE")
    wm_opacity = max(0, min(255, int(float(request.args.get("wm_opacity", 204)))))  # ≈80%
    wm_angle   = float(request.args.get("wm_angle", -30))
    wm_scale   = min(0.5, max(0.02, float(request.args.get("wm_scale", 0.06))))
    wm_mode    = (request.args.get("wm_mode", "cover") or "cover").lower()

    if not PIL_OK:
        resp = send_file(orig, mimetype="image/jpeg", as_attachment=False)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Preview-Method"] = "fallback-no-pillow"
        return resp

    from PIL import Image, ImageDraw, ImageFont
    with Image.open(orig) as im:
        im = im.convert("RGBA")
        w, h = im.size

        scale = min(1.0, max_side / float(max(w, h)))
        if scale < 1.0:
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            w, h = im.size

        font_size = max(20, int(w * wm_scale))
        font = None
        for path in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        ):
            if os.path.isfile(path):
                try:
                    font = ImageFont.truetype(path, font_size)
                    break
                except Exception:
                    pass
        if font is None:
            font = ImageFont.load_default()

        stroke = max(2, font_size // 14)
        dtmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        bbox = dtmp.textbbox((0, 0), wm_text, font=font, stroke_width=stroke)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

        pad = max(4, int(min(w, h) * 0.008))
        stamp = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
        ds = ImageDraw.Draw(stamp)
        ds.text(
            (pad - bbox[0], pad - bbox[1]),
            wm_text,
            font=font,
            fill=(255, 255, 255, wm_opacity),
            stroke_width=stroke,
            stroke_fill=(0, 0, 0, int(wm_opacity * 0.6)),
        )

        stamp_rot = stamp.rotate(wm_angle, expand=True, resample=Image.BICUBIC)
        sw, sh = stamp_rot.size

        gap_px = request.args.get("wm_gap")
        if gap_px is not None:
            gap = int(float(gap_px))
        else:
            gap_ratio = float(request.args.get("wm_gap_ratio", 0.35))
            gap_ratio = max(0.0, min(1.0, gap_ratio))
            gap = int(sw * gap_ratio)

        step_x, step_y = sw + gap, sh + gap
        overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))

        if wm_mode == "inside":
            default_margin = int(min(w, h) * 0.02)
            margin = request.args.get("wm_margin")
            margin = int(float(margin)) if margin is not None else default_margin
            margin = max(0, min(int(min(w, h) * 0.25), margin))

            avail_w = max(0, w - 2 * margin)
            avail_h = max(0, h - 2 * margin)

            nx = max(1, math.floor((avail_w - sw) / step_x) + 1)
            ny = max(1, math.floor((avail_h - sh) / step_y) + 1)

            used_w = (nx - 1) * step_x + sw
            used_h = (ny - 1) * step_y + sh

            start_x = margin + max(0, (avail_w - used_w) // 2)
            start_y = margin + max(0, (avail_h - used_h) // 2)
            for iy in range(ny):
                y = start_y + iy * step_y
                for ix in range(nx):
                    x = start_x + ix * step_x
                    overlay.paste(stamp_rot, (x, y), stamp_rot)
        else:
            total_w = w + 2 * sw
            total_h = h + 2 * sh
            nx = max(1, math.ceil((total_w - sw) / step_x) + 1)
            ny = max(1, math.ceil((total_h - sh) / step_y) + 1)
            used_w = (nx - 1) * step_x + sw
            used_h = (ny - 1) * step_y + sh
            start_x = - (used_w - w) // 2
            start_y = - (used_h - h) // 2
            for iy in range(ny):
                y = start_y + iy * step_y
                for ix in range(nx):
                    x = start_x + ix * step_x
                    overlay.paste(stamp_rot, (x, y), stamp_rot)

        out = Image.alpha_composite(im, overlay).convert("RGB")

    bio = io.BytesIO()
    out.save(bio, format="JPEG", quality=88, optimize=True, progressive=True)
    bio.seek(0)

    resp = send_file(bio, mimetype="image/jpeg", as_attachment=False, download_name="preview.jpg")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Preview-Method"] = "pillow-tile-cover"
    return resp
