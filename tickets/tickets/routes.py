# -*- coding: utf-8 -*-
import os
import json
import shutil
import secrets
from uuid import uuid4
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from flask import (
    request, render_template, redirect, url_for,
    flash, current_app, send_file, session, Response   # ← Response 追加
)

from PIL import Image, ExifTags  # Exif 読取

from . import tickets_bp
from app.utils.db import get_db  # 本体の DB 接続

# =========================
# 設定/ヘルパ
# =========================
TICKETS_PHOTO_ROOT = "/mnt/mfu/tickets_photo"   # 画像保存ルート
THUMB_QUEUE_DIR    = "/mnt/mfu/thumb_queue"     # サムネ生成キュー(JSON)

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def _pad(n: int, width: int = 4) -> str:
    return str(n).zfill(width)

def is_jpeg(mimetype: str, filename: str) -> bool:
    ext = os.path.splitext(filename.lower())[1]
    return (mimetype in ("image/jpeg", "image/pjpeg")) and ext in (".jpg", ".jpeg")

def _parse_exif_datetime(dt_str: str):
    """EXIF 'YYYY:MM:DD HH:MM:SS' -> datetime"""
    try:
        return datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        return None

def _get_shot_time(fullpath: str) -> datetime:
    """
    撮影時刻: Exif DateTimeOriginal -> DateTime -> ファイル mtime
    """
    try:
        with Image.open(fullpath) as im:
            exif = getattr(im, "_getexif", lambda: None)() or {}
            tagmap = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
            for key in ("DateTimeOriginal", "DateTime"):
                if key in tagmap:
                    dt = _parse_exif_datetime(str(tagmap[key]))
                    if dt:
                        return dt
    except Exception:
        pass
    ts = os.path.getmtime(fullpath)
    return datetime.fromtimestamp(ts)

def enqueue_thumbnail_job_for_batch(batch_id: int):
    """
    サムネイル生成ワーカーが監視する /mnt/mfu/thumb_queue に JSON を投下。
    mode='tickets', album_id=<batch_id>, child_id='-'
    """
    try:
        os.makedirs(THUMB_QUEUE_DIR, exist_ok=True)
        job = {
            "mode": "tickets",
            "album_id": str(batch_id),
            "child_id": "-"  # 未使用
        }
        name = f"tickets_{batch_id}_{uuid4().hex}.json"
        path = os.path.join(THUMB_QUEUE_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)
        current_app.logger.info(f"[THUMB-QUEUE] enqueued {path}")
        return True
    except Exception:
        current_app.logger.exception("enqueue_thumbnail_job_for_batch failed")
        return False

def _new_form_token() -> str:
    token = secrets.token_urlsafe(16)
    session['tickets_admin_token'] = token
    # 既に使ったトークンセットを保持（セッション内）
    used = session.get('tickets_admin_used_tokens', set())
    if isinstance(used, list):
        used = set(used)
    session['tickets_admin_used_tokens'] = list(used)[-20:]
    return token

def _consume_form_token(token: str) -> bool:
    saved = session.get('tickets_admin_token')
    if not token or token != saved:
        return False
    used = set(session.get('tickets_admin_used_tokens', []))
    if token in used:
        return False
    used.add(token)
    session['tickets_admin_used_tokens'] = list(used)[-20:]
    # 直後の再送信を防ぐため、新しいトークンに更新
    session['tickets_admin_token'] = secrets.token_urlsafe(16)
    return True

def _gen_short_code(batch_id: int, existing: set[str] | None = None) -> str:
    """
    短縮コード生成: <batch_id>-<8桁16進(小文字)>
    バッチ内での重複を避けるため existing セットを考慮して再生成。
    """
    existing = existing or set()
    while True:
        token8 = secrets.token_hex(4)  # 8桁16進（小文字）
        code = f"{batch_id}-{token8}"
        if code not in existing:
            return code

# =========================
# ✅ 追加：この BluePrint はログイン済みユーザーのみ許可
# =========================
@tickets_bp.before_request
def _tickets_login_required():
    """
    管理系のみログイン必須にする。
    公開系（/t/, /s/, /tickets/public, /tickets/api, /tickets/thumb, /tickets/preview, /tickets/dl）は除外。
    """
    p = request.path or ""

    # 公開プレフィックスはスキップ
    public_prefixes = (
        "/t/",                # 短縮コード入力・/t/<uuid>
        "/s/",                # 短縮コード直接
        "/tickets/public/",   # 互換
        "/tickets/api/",      # 公開API
        "/tickets/thumb/",    # サムネ配信
        "/tickets/preview/",  # プレビュー配信
        "/tickets/dl/",       # 生成ZIP配信
    )
    if p.startswith(public_prefixes):
        return  # ← 認証不要

    # それ以外（= 管理系 /tickets/admin など）はログイン必須
    if "user" not in session:
        return redirect(url_for("login"))

# =========================
# 管理：バッチ発行（GET: 画面表示 / POST: 作成→PRG）
# =========================
@tickets_bp.route("/tickets/admin", methods=["GET", "POST"])
def tickets_admin():
    created = []
    summary = None
    form = None

    if request.method == "POST":
        form = request.form

        # ---- 二重送信防止（ワンタイムトークン）----
        if not _consume_form_token(form.get("_token")):
            flash("同じ内容の送信を検知しました（再読み込み・戻る操作による重複防止）。", "warning")
            return redirect(url_for("tickets.tickets_admin"))

        try:
            event_name = (form.get("event_name") or "").strip()
            count = int(form.get("count") or "0")

            # 撮影日（管理用・任意）
            shoot_date_str = (form.get("shoot_date") or "").strip()

            # 閲覧開始/終了（終了=有効期限）
            view_start_str = (form.get("view_start") or "").strip()
            expires_at_str = (form.get("expires_at") or "").strip()

            # 制御パラメータ（未指定は既定値）
            max_downloads   = int(form.get("max_downloads")   or "10")
            # daily_limit は廃止 → DB には 0（無制限）で保存
            daily_limit     = 0
            cooldown_secs   = int(form.get("cooldown_secs")   or "60")
            zip_limit_files = int(form.get("zip_limit_files") or "100")
            # フォームは MB 指定、サーバ側で bytes に換算（MiB）
            zip_limit_mb    = int(form.get("zip_limit_mb")    or "1000")  # 既定 1000 MB ≒ 1GB
            zip_limit_bytes = zip_limit_mb * 1024 * 1024

            if not event_name:
                flash("イベント名は必須です。", "danger")
                return redirect(url_for("tickets.tickets_admin"))

            if count <= 0 or count > 2000:
                flash("発行枚数は 1〜2000 の範囲で指定してください。", "danger")
                return redirect(url_for("tickets.tickets_admin"))

            # 撮影日
            shoot_date = None
            if shoot_date_str:
                shoot_date = datetime.strptime(shoot_date_str, "%Y-%m-%d").date()

            # 閲覧開始日
            view_start = None
            if view_start_str:
                view_start = datetime.strptime(view_start_str, "%Y-%m-%d").date()

            # 有効期限（=閲覧終了日）
            expires_at = None
            if expires_at_str:
                dt = datetime.strptime(expires_at_str, "%Y-%m-%d")
                expires_at = datetime(dt.year, dt.month, dt.day, 23, 59, 59)

            # 終了未指定 & 開始あり → 開始+30日 23:59:59 を自動設定
            if expires_at is None and view_start is not None:
                end_date = view_start + timedelta(days=30)
                expires_at = datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59)

            db = get_db()
            cur = db.cursor(dictionary=True)

            # 1) ticket_batches
            cur.execute("""
                INSERT INTO ticket_batches
                    (event_name, shoot_date, view_start, expires_at,
                     max_downloads, daily_limit, cooldown_secs, zip_limit_files, zip_limit_bytes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (event_name, shoot_date, view_start, expires_at,
                  max_downloads, daily_limit, cooldown_secs, zip_limit_files, zip_limit_bytes))
            batch_id = cur.lastrowid

            # 2) tickets（連番 0001〜） + 短縮コード short_code 生成
            rows = []
            existing_codes = set()
            for i in range(1, count + 1):
                ticket_no = _pad(i, 4)
                uid = str(uuid4())
                short_code = _gen_short_code(batch_id, existing_codes)
                existing_codes.add(short_code)
                rows.append((batch_id, uid, ticket_no, short_code))

            # short_code カラムがある環境では同時INSERT、無い環境は古い定義でフォールバック
            try:
                cur.executemany("""
                    INSERT INTO tickets (batch_id, uuid, ticket_no, short_code)
                    VALUES (%s, %s, %s, %s)
                """, rows)
            except Exception:
                rows2 = [(r[0], r[1], r[2]) for r in rows]
                cur.executemany("""
                    INSERT INTO tickets (batch_id, uuid, ticket_no)
                    VALUES (%s, %s, %s)
                """, rows2)

            db.commit()
            db.close()

            flash(f"「{event_name}」に {count} 枚のチケットを発行しました。", "success")

            # ---- PRG: 成功後は GET にリダイレクト（再読み込みで再POSTされない）----
            return redirect(url_for("tickets.tickets_admin", created_batch_id=batch_id))

        except Exception as e:
            current_app.logger.exception("tickets_admin failed")
            flash(f"エラー: {e}", "danger")
            return redirect(url_for("tickets.tickets_admin"))

    # ---- GET: 画面表示（PRGの着地点）----
    created_batch_id = request.args.get("created_batch_id", type=int)

    # 一覧
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("""
        SELECT b.id, b.event_name, b.created_at,
               b.shoot_date, b.view_start, b.expires_at,
               COALESCE(cnt.cnt,0) AS file_count
        FROM ticket_batches b
        LEFT JOIN (
            SELECT batch_id, COUNT(*) AS cnt
            FROM ticket_files GROUP BY batch_id
        ) cnt ON cnt.batch_id = b.id
        ORDER BY b.id DESC
    """)
    batches = cur.fetchall()

    # 直近発行のサマリとチケット（PRGで指定されたときだけ）
    summary = None
    created = []
    if created_batch_id:
        cur.execute("SELECT * FROM ticket_batches WHERE id=%s", (created_batch_id,))
        b = cur.fetchone()
        if b:
            summary = {
                "batch_id": b["id"],
                "event_name": b["event_name"],
                "shoot_date": b["shoot_date"].isoformat() if b.get("shoot_date") else "(未設定)",
                "view_start": b["view_start"].isoformat() if b.get("view_start") else "(未設定)",
                "expires_at": b["expires_at"].strftime("%Y-%m-%d %H:%M:%S") if b.get("expires_at") else "(未設定)",
                "max_downloads": b.get("max_downloads"),
                "daily_limit": (b.get("daily_limit") or 0),  # 0 = 無制限
                "cooldown_secs": b.get("cooldown_secs"),
                "zip_limit_files": b.get("zip_limit_files"),
                "zip_limit_bytes": b.get("zip_limit_bytes"),
                "zip_limit_mb": int((b.get("zip_limit_bytes") or 0) // (1024 * 1024)),
            }
            cur.execute("""
                SELECT ticket_no, uuid
                FROM tickets
                WHERE batch_id=%s
                ORDER BY ticket_no ASC
                LIMIT 500
            """, (created_batch_id,))
            created = cur.fetchall()

    db.close()

    # 新しいフォームトークンを発行
    token = _new_form_token()

    return render_template(
        "ticket_admin.html",
        created=created,
        summary=summary,
        form=None,
        batches=batches,
        token=token,
    )

# =========================
# 管理：バッチ削除（DB+物理）
# =========================
@tickets_bp.route("/tickets/admin/batch/<int:batch_id>/delete", methods=["POST"])
def tickets_batch_delete(batch_id):
    """
    バッチ削除：
      - DB: ticket_batches（CASCADEで tickets/ticket_files/ticket_downloads も削除）
      - 物理: /mnt/mfu/tickets_photo/<batch_id>/ を削除
    """
    db = get_db()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT id, event_name FROM ticket_batches WHERE id=%s", (batch_id,))
    batch = cur.fetchone()
    if not batch:
        db.close()
        flash("対象のバッチが見つかりません。", "danger")
        return redirect(url_for("tickets.tickets_admin"))

    cur.execute("DELETE FROM ticket_batches WHERE id=%s", (batch_id,))
    db.commit()
    db.close()

    base_dir = os.path.join(TICKETS_PHOTO_ROOT, str(batch_id))
    try:
        if os.path.isdir(base_dir):
            shutil.rmtree(base_dir)
    except Exception as e:
        current_app.logger.exception("batch dir remove failed")
        flash(f"ディレクトリ削除で警告: {e}", "warning")

    flash(f"バッチ(ID={batch_id}, {batch['event_name']})を削除しました。", "success")
    return redirect(url_for("tickets.tickets_admin"))

# =========================
# 管理：アップロード（JPEG → original 保存 → サムネキュー投入 → 一覧）
# =========================
@tickets_bp.route("/tickets/admin/upload/<int:batch_id>", methods=["GET", "POST"])
def tickets_upload(batch_id):
    db = get_db()
    cur = db.cursor(dictionary=True)

    # バッチ情報
    cur.execute("SELECT * FROM ticket_batches WHERE id=%s", (batch_id,))
    batch = cur.fetchone()
    if not batch:
        db.close()
        flash("指定のバッチが見つかりません。", "danger")
        return redirect(url_for("tickets.tickets_admin"))

    # 保存ディレクトリ
    base_dir  = os.path.join(TICKETS_PHOTO_ROOT, str(batch_id))
    dir_orig  = os.path.join(base_dir, "original")
    dir_thumb = os.path.join(base_dir, "thumbnail")
    ensure_dir(dir_orig)
    ensure_dir(dir_thumb)

    if request.method == "POST":
        files = request.files.getlist("files")
        if not files:
            db.close()
            flash("ファイルが選択されていません。", "danger")
            return redirect(request.url)

        # 既存件数 + 1 を起点に連番付与（0001〜）
        cur.execute("SELECT COUNT(*) AS c FROM ticket_files WHERE batch_id=%s", (batch_id,))
        start_idx = (cur.fetchone() or {}).get("c", 0)

        saved = 0
        for f in files:
            if not f or f.filename == "":
                continue

            filename = secure_filename(f.filename)
            if not is_jpeg(f.mimetype, filename):
                flash(f"{filename} はJPEGではありません（拡張子/JPEG MIMEをご確認ください）。", "danger")
                continue

            # (1) 一旦一時名で保存
            tmp_name = f"{uuid4().hex}.jpg"
            fullpath_tmp = os.path.join(dir_orig, tmp_name)
            f.save(fullpath_tmp)

            # (2) 撮影時刻（写真ごとの管理用）
            shot_dt = _get_shot_time(fullpath_tmp)
            ymdhms = shot_dt.strftime("%Y%m%d_%H%M%S")

            # (3) 正式名: <batch_id>_<nnnn>_<yyyymmdd_HHMMss>.jpg
            seq = start_idx + saved + 1
            seq_str = str(seq).zfill(4)
            new_name = f"{batch_id}_{seq_str}_{ymdhms}.jpg"
            fullpath = os.path.join(dir_orig, new_name)

            # (4) リネーム
            os.replace(fullpath_tmp, fullpath)
            size_bytes = os.path.getsize(fullpath)

            # (5) DB 登録（shot_at を保存、サムネは .webp を想定）
            thumb_name = os.path.splitext(new_name)[0] + ".webp"
            cur.execute("""
                INSERT INTO ticket_files (batch_id, filename, path_original, path_thumb, size_bytes, shot_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                batch_id,
                new_name,
                fullpath,
                os.path.join(dir_thumb, thumb_name),
                size_bytes,
                shot_dt
            ))
            saved += 1

        db.commit()
        db.close()

        # バッチ単位でサムネ生成キュー投入（まとめて処理）
        enqueue_thumbnail_job_for_batch(batch_id)

        flash(f"{saved} 件のJPEGをアップロードしました。", "success")
        return redirect(request.url)

    # GET: 一覧表示（最新500件）
    cur.execute("""
        SELECT id, filename, path_thumb, size_bytes, shot_at, created_at
        FROM ticket_files
        WHERE batch_id=%s
        ORDER BY id DESC
        LIMIT 500
    """, (batch_id,))
    files = cur.fetchall()
    db.close()

    # サムネURL（.webp）生成
    for r in files:
        webp_name = os.path.splitext(r["filename"])[0] + ".webp"
        r["thumb_url"] = url_for("tickets.ticket_thumb_serve", batch_id=batch_id, filename=webp_name)

    return render_template("ticket_upload.html", batch=batch, files=files)

# =========================
# 開発用：サムネ配信（本番は Web サーバの静的配信推奨）
# =========================
@tickets_bp.route("/tickets/admin/thumb/<int:batch_id>/<path:filename>")
def ticket_thumb_serve(batch_id, filename):
    path = os.path.join(TICKETS_PHOTO_ROOT, str(batch_id), "thumbnail", filename)
    if not os.path.isfile(path):
        return ("", 404)
    return send_file(path, mimetype="image/webp")

# =========================
# 追加：チケットUUID/短縮コード CSV ダウンロード
# =========================
@tickets_bp.route("/tickets/admin/batch/<int:batch_id>/tickets.csv")
def tickets_csv(batch_id: int):
    """
    指定バッチの ticket_no, uuid, short_code をCSVでダウンロード
    カラムが無い環境でも落ちないよう short_code はフォールバック。
    """
    db = get_db()
    cur = db.cursor(dictionary=True)

    # バッチ存在確認（任意：イベント名をファイル名に使いたい場合に取得）
    cur.execute("SELECT id, event_name FROM ticket_batches WHERE id=%s", (batch_id,))
    batch = cur.fetchone()
    if not batch:
        db.close()
        return Response("batch not found", status=404)

    # short_code を取れるなら取得、ダメなら従来列のみ
    try:
        cur.execute("""
            SELECT ticket_no, uuid, short_code
              FROM tickets
             WHERE batch_id=%s
             ORDER BY ticket_no ASC
        """, (batch_id,))
        rows = cur.fetchall()
        has_short = True
    except Exception:
        cur.execute("""
            SELECT ticket_no, uuid
              FROM tickets
             WHERE batch_id=%s
             ORDER BY ticket_no ASC
        """, (batch_id,))
        rows = cur.fetchall()
        has_short = False

    db.close()

    # UTF-8 with BOM（Excelでの文字化け回避）
    if has_short:
        lines = ["ticket_no,uuid,short_code"]
        for r in rows:
            lines.append(f"{r.get('ticket_no')},{r.get('uuid')},{r.get('short_code') or ''}")
    else:
        lines = ["ticket_no,uuid"]
        for r in rows:
            lines.append(f"{r.get('ticket_no')},{r.get('uuid')}")

    csv_text = "\n".join(lines)
    data = ("\ufeff" + csv_text).encode("utf-8")

    filename = f"tickets_{batch_id}.csv"
    return Response(
        data,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )

# === 追加：バッチ設定の編集 ===
from datetime import datetime, timedelta

# 既存: バッチ編集
# =========================================================
# バッチ設定の編集 + 使用状況（先頭500件表示 & リセット）
# =========================================================
@tickets_bp.route(
    "/tickets/admin/batch/<int:batch_id>/edit",
    methods=["GET", "POST"],
    endpoint="tickets_batch_edit"  # 一意名を明示
)
def tickets_batch_edit(batch_id: int):
    db = get_db()
    cur = db.cursor(dictionary=True)

    # ---- バッチ取得 ----
    cur.execute("SELECT * FROM ticket_batches WHERE id=%s", (batch_id,))
    batch = cur.fetchone()
    if not batch:
        db.close()
        flash("対象のバッチが見つかりません。", "danger")
        return redirect(url_for("tickets.tickets_admin"))

    if request.method == "POST":
        form = request.form

        # 二重送信防止（CSRF/ワンタイム）
        try:
            token_ok = _consume_form_token(form.get("_token"))
        except NameError:
            # 万一ユーティリティ未導入でも動くようフォールバック
            token_ok = True

        if not token_ok:
            db.close()
            flash("同じ内容の送信を検知しました（再送信防止）。", "warning")
            return redirect(request.url)

        try:
            # ---- 入力取り出し ----
            expires_at_str  = (form.get("expires_at") or "").strip()
            max_downloads   = int(form.get("max_downloads") or "0")
            cooldown_secs   = int(form.get("cooldown_secs") or "0")
            zip_limit_files = int(form.get("zip_limit_files") or "0")
            zip_limit_mb    = int(form.get("zip_limit_mb") or "0")
            public_usage_message = (form.get("public_usage_message") or "").strip()

            # ---- バリデーション ----
            if max_downloads < 0:
                raise ValueError("最大DL回数は0以上で指定してください。")
            if cooldown_secs < 0:
                raise ValueError("クールダウン秒は0以上で指定してください。")
            if zip_limit_files < 0:
                raise ValueError("ZIP枚数上限は0以上で指定してください。")
            if zip_limit_mb < 0:
                raise ValueError("ZIP容量上限(MB)は0以上で指定してください。")

            # ---- 変換 ----
            # 1MB = 1024 * 1024 B（MiB換算）で bytes へ
            zip_limit_bytes = zip_limit_mb * 1024 * 1024

            # 閲覧終了日は空なら NULL
            expires_at = None
            if expires_at_str:
                # datetime-local: 'YYYY-MM-DDTHH:MM'
                expires_at = datetime.strptime(expires_at_str, "%Y-%m-%dT%H:%M")

            # ---- UPDATE ----
            cur.execute("""
                UPDATE ticket_batches
                   SET expires_at=%s,
                       max_downloads=%s,
                       cooldown_secs=%s,
                       zip_limit_files=%s,
                       zip_limit_bytes=%s,
                       public_usage_message=%s
                 WHERE id=%s
            """, (
                expires_at, max_downloads, cooldown_secs,
                zip_limit_files, zip_limit_bytes,
                public_usage_message, batch_id
            ))
            db.commit()
            db.close()

            flash("バッチ設定を更新しました。", "success")
            # PRG: 自画面へ戻る
            return redirect(url_for("tickets.tickets_batch_edit", batch_id=batch_id))

        except Exception as e:
            db.rollback()
            current_app.logger.exception("tickets_batch_edit failed")
            db.close()
            flash(f"更新に失敗しました: {e}", "danger")
            return redirect(request.url)

    # ---- GET：フォーム値整形 ----
    def _to_dt_local(dt):
        # input[type=datetime-local] 用：'YYYY-MM-DDTHH:MM'
        return dt.strftime("%Y-%m-%dT%H:%M") if dt else ""

    formdata = {
        "batch_id": batch["id"],
        "event_name": batch.get("event_name") or "",
        "expires_at": _to_dt_local(batch.get("expires_at")),
        "max_downloads": batch.get("max_downloads") or 0,
        "cooldown_secs": batch.get("cooldown_secs") or 0,
        "zip_limit_files": batch.get("zip_limit_files") or 0,
        # bytes → MiB換算（floor）
        "zip_limit_mb": int((batch.get("zip_limit_bytes") or 0) // (1024 * 1024)),
        "public_usage_message": batch.get("public_usage_message") or "",
    }

    # ---- 先頭500件: チケット使用状況を取得（短縮コード含む） ----
    usage_supported = True
    tickets_rows = []
    try:
        cur.execute("""
            SELECT id, ticket_no, uuid, short_code, download_count, last_download_at,
                   COALESCE(reset_count, 0) AS reset_count
              FROM tickets
             WHERE batch_id=%s
             ORDER BY ticket_no ASC
             LIMIT 500
        """, (batch_id,))
        tickets_rows = cur.fetchall()
    except Exception:
        # download_count / short_code 等が無い環境でも落とさない
        usage_supported = False
        cur.execute("""
            SELECT id, ticket_no, uuid, short_code
              FROM tickets
             WHERE batch_id=%s
             ORDER BY ticket_no ASC
             LIMIT 500
        """, (batch_id,))
        tickets_rows = cur.fetchall()

    def _fmt(dt):
        if not dt:
            return ""
        try:
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(dt)

    max_dl = formdata["max_downloads"] or 0
    cd_secs = formdata["cooldown_secs"] or 0

    tickets_view = []
    for r in tickets_rows:
        used = (r.get("download_count") or 0) if usage_supported else None
        last = r.get("last_download_at") if usage_supported else None

        next_at = None
        if usage_supported and last and cd_secs > 0:
            try:
                next_at = last + timedelta(seconds=int(cd_secs))
            except Exception:
                next_at = None

        remaining = None
        progress_pct = None
        if usage_supported and max_dl and max_dl > 0:
            remaining = max(0, max_dl - used)
            try:
                progress_pct = min(100, int(used * 100 / max_dl))
            except Exception:
                progress_pct = None

        tickets_view.append({
            "id": r["id"],
            "ticket_no": r.get("ticket_no"),
            "uuid": r.get("uuid"),
            "short_code": r.get("short_code"),  # ★ 追加：短縮コード
            "used": used,
            "remaining": remaining,
            "progress_pct": progress_pct,
            "last_str": _fmt(last),
            "next_at_str": _fmt(next_at),
            "reset_count": r.get("reset_count", 0) if usage_supported else None,
        })

    try:
        token = _new_form_token()
    except NameError:
        token = ""  # フォールバック

    db.close()
    return render_template(
        "ticket_batch_edit.html",
        form=formdata, token=token,
        usage_supported=usage_supported,
        tickets=tickets_view
    )


# =========================================================
# 個別チケットの使用回数リセット
# =========================================================
@tickets_bp.route(
    "/tickets/admin/ticket/<int:ticket_id>/reset",
    methods=["POST"],
    endpoint="tickets_ticket_reset"
)
def tickets_ticket_reset(ticket_id: int):
    db = get_db()
    cur = db.cursor(dictionary=True)

    form = request.form
    try:
        token_ok = _consume_form_token(form.get("_token"))
    except NameError:
        token_ok = True

    if not token_ok:
        db.close()
        flash("不正または重複送信を検知しました。", "warning")
        return redirect(url_for("tickets.tickets_admin"))

    # リダイレクト先のため、batch_id を取得
    cur.execute("SELECT id, batch_id FROM tickets WHERE id=%s", (ticket_id,))
    t = cur.fetchone()
    if not t:
        db.close()
        flash("チケットが見つかりません。", "danger")
        return redirect(url_for("tickets.tickets_admin"))

    try:
        # reset_count が無い環境でも動くよう二段構え
        try:
            cur.execute("""
                UPDATE tickets
                   SET download_count=0,
                       last_download_at=NULL,
                       reset_count=COALESCE(reset_count,0)+1
                 WHERE id=%s
            """, (ticket_id,))
        except Exception:
            cur.execute("""
                UPDATE tickets
                   SET download_count=0,
                       last_download_at=NULL
                 WHERE id=%s
            """, (ticket_id,))
        db.commit()
        flash("使用回数をリセットしました。", "success")
    except Exception as e:
        db.rollback()
        current_app.logger.exception("tickets_ticket_reset failed")
        flash(f"リセットに失敗しました: {e}", "danger")
    finally:
        db.close()

    return redirect(url_for("tickets.tickets_batch_edit", batch_id=t["batch_id"]) + "#usage")
