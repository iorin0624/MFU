# /mnt/mfu/app/albums/api_ext.py
# Windowsクライアント（GUI）からアルバムへ直アップロードするAPI（Bearer認証）
# 既存の画面テンプレートや既存ルートは変更しません。

from __future__ import annotations
import os
import re
import time
from datetime import datetime
from typing import List, Dict

from flask import Blueprint, request, jsonify, abort, current_app, url_for

import shutil
from werkzeug.utils import secure_filename
import secrets

# 認証キー：環境変数 MFU_EXT_API_KEY（未設定なら認証スキップ）
API_KEY = os.environ.get("MFU_EXT_API_KEY", "").strip()

# 既存アルバム実装から最低限の関数を利用
#   - ストレージディレクトリの切り替え / 命名規則 / 許可拡張子 / サムネ生成キュー
#   - EXIF日時 → ファイル名の一部に反映
from app.albums.routes import (           # ✅ 既存実装の流用（テンプレ変更なし）
    load_meta,                             # メタ取得 (DB)
    storage_child_dir,                     # 子アルバムの保存ルート決定
    allowed_file, allowed_movie,           # 拡張子チェック
)
from app.utils.thumbs import enqueue_thumb_job
from app.albums.photo_namer import get_datetime_from_image

album_api_up = Blueprint("album_api_up", __name__, url_prefix="/api/albums")

def _auth_required():
    if not API_KEY:
        return                                  # 認証スキップ（LAN内テスト等）
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        abort(401)
    if auth.split(" ", 1)[1] != API_KEY:
        abort(403)

def _scan_next_seq_normal(child_path: str, child_id: str) -> int:
    # child_id_YYYYmmdd_HHMMSS_0001.jpg の末尾連番から次番号を決める
    xs = []
    for f in os.listdir(child_path):
        if not f.startswith(child_id + "_"):
            continue
        try:
            tail = f.split("_")[-1]
            n = os.path.splitext(tail)[0]
            xs.append(int(n))
        except Exception:
            pass
    return (max(xs) + 1) if xs else 1

def _scan_next_seq_movie(child_path: str, child_id: str) -> int:
    # {child_id}_YYYYmmdd_HHMM_NNNN.ext の NNNN 最大から次番号
    pat = re.compile(rf"^{re.escape(child_id)}_\d{{8}}_\d{{4}}_(\d{{4}})\.[A-Za-z0-9]+$")
    seq = 1
    for f in os.listdir(child_path):
        m = pat.match(f)
        if m:
            try:
                seq = max(seq, int(m.group(1)) + 1)
            except Exception:
                pass
    return seq

def _ok(meta: Dict) -> bool:
    return bool(meta and meta.get("children"))

@album_api_up.route("/children", methods=["GET"])
def list_children():
    """
    子アルバム一覧取得API
    - params: album_id (必須)
    - return: {children: [{name, folder, mode}], album_name}
    """
    _auth_required()
    album_id = (request.args.get("album_id") or "").strip()
    if not album_id:
        return jsonify({"ok": False, "error": "missing album_id"}), 400

    meta = load_meta(album_id)
    if not _ok(meta):
        return jsonify({"ok": False, "error": "album not found"}), 404

    return jsonify({
        "ok": True,
        "album_id": album_id,
        "album_name": meta.get("album_name", ""),
        "children": meta.get("children", []),
    })

@album_api_up.route("/upload", methods=["POST"])
def upload_to_child():
    """
    アップロードAPI（画像/動画/加工モードに対応）
    form-data:
      - album_id (必須)
      - child_id (必須)
      - generate_thumbs = 1|0 （省略時1、normal時のみ有効）
      - files[] （複数可）
    動画モード: 既存命名 {child_id}_YYYYmmdd_HHMM_NNNN.ext
    画像モード: 既存命名 {child_id}_YYYYmmdd_HHMMSS_NNNN.ext（EXIF日時優先）
    """
    _auth_required()

    album_id = (request.form.get("album_id") or "").strip()
    child_id = (request.form.get("child_id") or "").strip()
    if not album_id or not child_id:
        return jsonify({"ok": False, "error": "missing album_id/child_id"}), 400

    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "no files"}), 400

    meta = load_meta(album_id)
    if not _ok(meta):
        return jsonify({"ok": False, "error": "album not found"}), 404

    child = next((c for c in meta["children"] if c.get("folder") == child_id), None)
    if not child:
        return jsonify({"ok": False, "error": "child not found"}), 404

    mode = (child.get("mode") or "normal").lower()
    child_path = storage_child_dir(album_id, child_id, mode)
    os.makedirs(child_path, exist_ok=True)

    saved: List[str] = []

    # ---- movie: 連番 + 分精度スタンプ ----
    if mode == "movie":
        stamp = time.strftime("%Y%m%d_%H%M")
        seq = _scan_next_seq_movie(child_path, child_id)
        for i, f in enumerate(files):
            if not f or not f.filename:
                continue
            ext = secure_filename(f.filename).rsplit(".", 1)[-1].lower()
            if not allowed_movie(f.filename):
                continue
            name = f"{child_id}_{stamp}_{(seq+i):04d}.{ext}"
            f.save(os.path.join(child_path, name))
            saved.append(name)
        # 変換/ポスターは既存のバックグラウンドが走る（view時に .web.mp4 優先表示）
        # ※変換の起動は albums/routes 側に委任（既存挙動） :contentReference[oaicite:2]{index=2}

    # ---- process: 最新1枚差し替え ----
    elif mode == "process":
        from PIL import Image
        # latest.* を置換、履歴は既存ルートのまま扱う（GUI からは保存のみ）
        f0 = files[0]
        save_path = os.path.join(child_path, "latest.jpg")
        try:
            img = Image.open(f0.stream).convert("RGB")
            img.save(save_path, format="JPEG")
        except Exception:
            f0.stream.seek(0)
            f0.save(save_path)
        saved.append("latest.jpg")

    # ---- normal: EXIF日時 + 連番 ----
    else:
        next_seq = _scan_next_seq_normal(child_path, child_id)
        for i, f in enumerate(files):
            if not f or not f.filename or not allowed_file(f.filename):
                continue
            ext = secure_filename(f.filename).rsplit(".", 1)[-1].lower()
            dt = get_datetime_from_image(f)  # 例: 20240516_153215（EXIF優先） :contentReference[oaicite:3]{index=3}
            f.stream.seek(0)
            name = f"{child_id}_{dt}_{(next_seq+i):04d}.{ext}"
            f.save(os.path.join(child_path, name))
            saved.append(name)

        # サムネ生成キュー（既存ワーカーへ投げる）
        gen = (request.form.get("generate_thumbs", "1").lower() in ("1", "true", "yes"))
        if gen:
            try:
                enqueue_thumb_job("album", album_id, child_id)
            except Exception as e:
                current_app.logger.warning("enqueue_thumb_job failed: %s", e)

    view_url = url_for("album.view_child", album_id=album_id, child_id=child_id, _external=True)
    return jsonify({"ok": True, "album_id": album_id, "child_id": child_id, "mode": mode, "saved": saved, "view_url": view_url})

@album_api_up.route("/ping", methods=["GET"])
def ping():
    _auth_required()
    return jsonify({"ok": True, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})

# === ここから追記: 親アルバム一覧 / 親作成 / 子作成 API =======================
# 既存実装の関数/定数をそのまま流用します（DB・保存先・token生成など）
# - create_album_row(), add_child_row(), list_albums_for_user()
# - storage_child_dir(), ALBUM_ROOT, MOVIE_ROOT
from app.albums.routes import (
    create_album_row, add_child_row, list_albums_for_user,
    storage_child_dir, ALBUM_ROOT, MOVIE_ROOT, load_meta
)  # ← 既存 routes の関数・定数を利用（テンプレは一切変更しない）  # :contentReference[oaicite:2]{index=2}

import uuid
import os

@album_api_up.route("/list", methods=["GET"])
def list_albums():
    """
    親アルバム一覧取得
    GET /api/albums/list?owner=<username>
      - owner: 必須（例: admin や iori など）
    戻り:
      { ok: true, owner: "admin", albums: [{id, name}] }
    """
    _auth_required()
    owner = (request.args.get("owner") or "").strip()
    if not owner:
        return jsonify({"ok": False, "error": "missing owner"}), 400

    # 既存の一覧取得ヘルパを流用（所有者ごと）  # :contentReference[oaicite:3]{index=3}
    rows = list_albums_for_user(owner) or []
    albums = [{"id": r["id"], "name": r["name"]} for r in rows]
    return jsonify({"ok": True, "owner": owner, "albums": albums})


@album_api_up.route("/create", methods=["POST"])
def create_parent_album():
    """
    親アルバムの新規作成
    POST /api/albums/create
    JSON: { name: "2025春オフ会", owner: "admin" }
    戻り: { ok: true, album_id, access_token, access_url }
    """
    _auth_required()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    owner = (data.get("owner") or "").strip()
    if not name or not owner:
        return jsonify({"ok": False, "error": "missing name/owner"}), 400

    album_id = str(uuid.uuid4())

    # 画像/動画ルートを既存の保存先に合わせて作成  # :contentReference[oaicite:4]{index=4}
    os.makedirs(os.path.join(ALBUM_ROOT, album_id), exist_ok=True)
    os.makedirs(os.path.join(MOVIE_ROOT, album_id), exist_ok=True)

    # DB 登録（トークン生成も既存関数で）  # :contentReference[oaicite:5]{index=5}
    access_token = create_album_row(album_id, name, owner)

    # 既存のアクセスルートは album_bp にあるため URL はクライアントで組み立て想定
    return jsonify({
        "ok": True,
        "album_id": album_id,
        "access_token": access_token
    })


@album_api_up.route("/create_child", methods=["POST"])
def create_child_album():
    """
    子アルバムの新規作成（normal モード固定）
    POST /api/albums/create_child
    JSON: { album_id: "<uuid>", name: "日付_会場_一次会" }
    戻り: { ok: true, album_id, child_id, mode: "normal" }
    """
    _auth_required()
    data = request.get_json(silent=True) or {}
    album_id = (data.get("album_id") or "").strip()
    name = (data.get("name") or "").strip()
    if not album_id or not name:
        return jsonify({"ok": False, "error": "missing album_id/name"}), 400

    meta = load_meta(album_id)
    if not meta:
        return jsonify({"ok": False, "error": "album not found"}), 404

    # 既存の DB 追加関数を利用（mode='normal'）  # :contentReference[oaicite:6]{index=6}
    child_id = add_child_row(album_id, name, "normal")

    # 実ストレージも既存のディレクトリ規約で作成  # :contentReference[oaicite:7]{index=7}
    os.makedirs(storage_child_dir(album_id, child_id, "normal"), exist_ok=True)

    return jsonify({"ok": True, "album_id": album_id, "child_id": child_id, "mode": "normal"})
# === 追記ここまで ============================================================
