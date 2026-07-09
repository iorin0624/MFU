import os
import glob
import json
import threading
import time
from functools import wraps
from flask import Blueprint, request, jsonify, render_template, session, abort

from app.utils.preset_store import load_presets, add_preset, delete_preset
from app.utils.timer_engine import (
    start_timer, get_active_timers, cancel_timer,
    set_alarm, get_scheduled_alarms, cancel_alarm,
    get_timer_settings, set_timer_settings,
    list_voicevox_speakers, test_speak_once,  # 既存テスト（TTS単体）
    test_preview,                              # 追加：チャイム+TTSの複合プレビュー
)
from app.utils.jan_mapping import (
    get_timer_by_jan, get_info_by_jan,
    upsert_mapping, list_mapping, delete_mapping
)

timer_bp = Blueprint("timer", __name__)


def timer_admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if session.get("user") != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "admin_required"}), 403
            abort(403)
        return func(*args, **kwargs)

    return wrapper


def _is_loopback_request() -> bool:
    return (request.remote_addr or "") in {"127.0.0.1", "::1"}


def timer_admin_or_loopback_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if session.get("user") == "admin" or _is_loopback_request():
            return func(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "admin_required"}), 403
        abort(403)
    return wrapper

# --------------------
# pending タイマー一時保存用（QR/バーコードリーダー連携）
# --------------------
PENDING_TIMER_FILE = "/mnt/mfu/data/pending_timer.json"
_pending_lock = threading.RLock()


def _load_pending_timer() -> dict:
    try:
        with open(PENDING_TIMER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_pending_timer(data: dict) -> None:
    os.makedirs(os.path.dirname(PENDING_TIMER_FILE), exist_ok=True)
    tmp = PENDING_TIMER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PENDING_TIMER_FILE)


# --------------------
# UIルート
# --------------------
@timer_bp.route("/admin/timer")
@timer_admin_required
def admin_timer():
    # テンプレート名は変更しない
    return render_template("admin_timer.html")

# --------------------
# API: タイマー関連
# --------------------
@timer_bp.route("/api/timer/start", methods=["POST"])
@timer_admin_required
def api_timer_start():
    data = request.get_json(force=True, silent=True) or {}
    try:
        seconds = int(data.get("seconds", 0))
        label = (data.get("label") or "タイマー").strip()
        if seconds <= 0:
            return jsonify({"error": "秒数は1以上を指定してください"}), 400
        start_timer(seconds, label)
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@timer_bp.route("/api/timer/status")
def api_timer_status():
    return jsonify(get_active_timers())


@timer_bp.route("/api/timer/cancel", methods=["POST"])
@timer_admin_required
def api_timer_cancel():
    data = request.get_json(force=True, silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "ラベルを指定してください"}), 400
    cancel_timer(label)
    return jsonify({"status": "ok"})

# --------------------
# API: pending タイマー制御（MFU本体USBのQR/バーコードリーダー用）
# --------------------
@timer_bp.route("/api/timer/pending-set", methods=["POST"])
@timer_admin_or_loopback_required
def api_timer_pending_set():
    """
    body: { "seconds": 60, "label": "1分" } など
    QRコードで「1」「2」「30S」等を読み取ったスクリプトから叩く想定。
    """
    data = request.get_json(force=True, silent=True) or {}
    try:
        seconds = int(data.get("seconds", 0))
    except Exception:
        return jsonify({"error": "秒数が不正です"}), 400

    label = (data.get("label") or "").strip() or f"{seconds}秒"

    if seconds <= 0:
        return jsonify({"error": "秒数が不正です"}), 400

    payload = {"label": label, "seconds": seconds}

    with _pending_lock:
        _save_pending_timer(payload)

    return jsonify({"status": "ok", "pending": payload})


@timer_bp.route("/api/timer/pending-set-by-jan", methods=["POST"])
@timer_admin_or_loopback_required
def api_timer_pending_set_by_jan():
    """
    body: { "jan": "4901234567890" }
    JANコードを読み取ったスクリプトから叩く。
    get_timer_by_jan() の結果を pending に保存するだけで、まだ起動しない。
    """
    data = request.get_json(force=True, silent=True) or {}
    jan = (data.get("jan") or "").strip()

    if not jan:
        return jsonify({"error": "JANが空です"}), 400

    hit = get_timer_by_jan(jan)
    if not hit:
        return jsonify({"status": "not_found", "message": "未登録のJANです"}), 404

    label, seconds = hit
    payload = {"label": label, "seconds": int(seconds)}

    with _pending_lock:
        _save_pending_timer(payload)

    return jsonify({"status": "ok", "pending": payload})


@timer_bp.route("/api/timer/pending-start", methods=["POST"])
@timer_admin_or_loopback_required
def api_timer_pending_start():
    """
    body: {} でOK。
    pending_timer.json に待機中のタイマーがあれば start_timer() を呼んで起動する。
    START用QRコードから叩く想定。
    """
    with _pending_lock:
        pending = _load_pending_timer()

    if not pending:
        return jsonify({"error": "待機中のタイマーがありません"}), 400

    label = (pending.get("label") or "タイマー").strip()
    try:
        seconds = int(pending.get("seconds", 0))
    except Exception:
        return jsonify({"error": "pendingタイマーの秒数が不正です"}), 500

    if seconds <= 0:
        return jsonify({"error": "pendingタイマーの秒数が不正です"}), 500

    try:
        start_timer(seconds, label)
        # スタートに成功したら pending をクリア
        with _pending_lock:
            _save_pending_timer({})
        return jsonify({"status": "ok", "label": label, "seconds": seconds})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --------------------
# API: プリセット関連
# --------------------
@timer_bp.route("/api/preset")
@timer_admin_required
def api_get_presets():
    return jsonify(load_presets())


@timer_bp.route("/api/preset/add", methods=["POST"])
@timer_admin_required
def api_add_preset():
    data = request.get_json(force=True, silent=True) or {}
    try:
        label = (data.get("label") or "").strip()
        seconds = int(data.get("seconds", 0))
        if not label or seconds <= 0:
            return jsonify({"error": "ラベルと秒数を正しく指定してください"}), 400
        success = add_preset(label, seconds)
        return jsonify({"status": "ok" if success else "duplicate"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@timer_bp.route("/api/preset/delete", methods=["POST"])
@timer_admin_required
def api_delete_preset():
    data = request.get_json(force=True, silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "ラベルを指定してください"}), 400
    success = delete_preset(label)
    return jsonify({"status": "ok" if success else "not_found"})

# --------------------
# API: アラーム関連
# --------------------
@timer_bp.route("/api/alarm/set", methods=["POST"])
@timer_admin_required
def api_set_alarm():
    data = request.get_json(force=True, silent=True) or {}
    try:
        hour = int(data.get("hour", -1))
        minute = int(data.get("minute", -1))
        label = (data.get("label") or "アラーム").strip()

        if not (0 <= hour < 24) or not (0 <= minute < 60):
            return jsonify({"error": "時刻の指定が不正です"}), 400

        alarm_id = set_alarm(hour, minute, label)
        return jsonify({"status": "ok", "id": alarm_id})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@timer_bp.route("/api/alarms")
@timer_admin_required
def api_get_alarms():
    # 期限切れは内部で掃除され、未来のみが返る
    return jsonify(get_scheduled_alarms())


@timer_bp.route("/api/alarm/cancel", methods=["POST"])
@timer_admin_required
def api_alarm_cancel():
    data = request.get_json(force=True, silent=True) or {}
    label = (data.get("label") or "").strip()
    if not label:
        return jsonify({"error": "ラベルを指定してください"}), 400
    success = cancel_alarm(label)
    return jsonify({"status": "ok" if success else "not_found"})

# --------------------
# API: JAN → タイマー起動
# --------------------
@timer_bp.route("/api/timer/jan-start", methods=["POST"])
@timer_admin_required
def api_timer_start_by_jan():
    data = request.get_json(force=True, silent=True) or {}
    jan = (data.get("jan") or "").strip()
    hit = get_timer_by_jan(jan)
    if not hit:
        return jsonify({"status": "not_found", "message": "未登録のJANです"}), 404
    label, seconds = hit
    try:
        start_timer(seconds, label)
        return jsonify({"status": "ok", "label": label, "seconds": seconds})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# （任意）UIから辞書を増やしたいとき
@timer_bp.route("/api/timer/jan-upsert", methods=["POST"])
@timer_admin_required
def api_timer_jan_upsert():
    data = request.get_json(force=True, silent=True) or {}

    jan   = (data.get("jan")   or "").strip()
    label = (data.get("label") or "").strip()

    # seconds の変換とバリデーション
    try:
        seconds = int(data.get("seconds", 0))
    except Exception:
        return jsonify({
            "status": "error",
            "message": "秒数が不正です"
        }), 400

    if not jan or not label or seconds <= 0:
        return jsonify({
            "status": "error",
            "message": "入力値を確認してください"
        }), 400

    try:
        ok = upsert_mapping(jan, label, seconds)
    except Exception as e:
        # jan_mapping 側で例外が出た場合も JSON で返す
        return jsonify({
            "status": "error",
            "message": str(e) or "内部エラーが発生しました"
        }), 500

    if ok:
        return jsonify({"status": "ok"})

    return jsonify({
        "status": "error",
        "message": "入力値を確認してください"
    }), 400

# 情報だけ返す（起動しない）
@timer_bp.route("/api/timer/jan-info", methods=["POST"])
@timer_admin_required
def api_timer_jan_info():
    data = request.get_json(force=True, silent=True) or {}
    jan = (data.get("jan") or "").strip()
    info = get_info_by_jan(jan)
    if not info:
        return jsonify({"status": "not_found"}), 404
    # 返す：label, seconds, steps(任意)
    return jsonify({"status": "ok", **info})

# --------------------
# API: JAN 一覧・削除（UI用）
# --------------------
@timer_bp.route("/api/timer/jan-list")
@timer_admin_required
def api_timer_jan_list():
    # 例外時も空オブジェクトを返すとUIが落ちにくい
    try:
        data = list_mapping() or {}
        return jsonify(data)
    except Exception:
        return jsonify({}), 200


@timer_bp.route("/api/timer/jan-delete", methods=["POST"])
@timer_admin_required
def api_timer_jan_delete():
    data = request.get_json(force=True, silent=True) or {}
    jan = (data.get("jan") or "").strip()
    if not jan:
        return jsonify({"status": "error", "message": "JANが空です"}), 400
    ok = delete_mapping(jan)
    return jsonify({"status": "ok" if ok else "not_found"})

# --------------------
# API: 設定（通知音＆回数）
# --------------------
@timer_bp.route("/api/timer/settings", methods=["GET", "POST"])
@timer_admin_required
def api_timer_settings():
    if request.method == "GET":
        return jsonify(get_timer_settings())
    data = request.get_json(force=True, silent=True) or {}
    updated = set_timer_settings(data)
    return jsonify(updated)


@timer_bp.route("/api/timer/available-sounds", methods=["GET"])
@timer_admin_required
def api_timer_available_sounds():
    base = "/mnt/mfu/app/sound"
    exts = (".wav", ".mp3", ".ogg")
    files = []
    try:
        for p in sorted(glob.glob(os.path.join(base, "**", "*"), recursive=True)):
            if os.path.isfile(p) and p.lower().endswith(exts):
                files.append(p)
    except Exception:
        pass
    return jsonify({"sounds": files})

# --------------------
# API: VOICEVOX（一覧・テスト）
# --------------------
@timer_bp.route("/api/timer/voicevox/speakers", methods=["GET"])
@timer_admin_required
def api_timer_voicevox_speakers():
    try:
        data = list_voicevox_speakers()
        return jsonify(data)
    except Exception:
        # エラー時も空配列を返す
        return jsonify([]), 200


# 既存：TTSのみの単純テスト
@timer_bp.route("/api/timer/voicevox/test-speak", methods=["POST"])
@timer_admin_required
def api_timer_voicevox_test_speak():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip() or "テストです。"
    # SE02 デフォルト話者：東北きりたん（108）
    speaker = int(
        data.get("speaker_id")
        or get_timer_settings().get("speaker_id")
        or 108
    )
    ok = test_speak_once(text, speaker)
    return jsonify({"ok": bool(ok), "speaker_id": speaker})

# 新規：チャイム+VOICEVOXの組み合わせまで含めたプレビュー再生
@timer_bp.route("/api/timer/preview", methods=["POST"])
@timer_admin_required
def api_timer_preview():
    """
    受け取るJSON:
      {
        "text": "テストです。",
        "mode": "chime_vv" | "vv_only" | "chime_only",
        "chime_path": "/mnt/mfu/app/sound/xxx.wav",
        "chime_repeat": 2,
        "speaker_id": 108,
        "tts_repeat": 2
      }
    未指定は現在の設定値を既定にする。
    """
    data = request.get_json(force=True, silent=True) or {}
    st = get_timer_settings()

    text = (data.get("text") or "").strip() or "テストです。"
    mode = (data.get("mode") or st.get("mode") or "chime_vv").lower()
    chime_path = (data.get("chime_path") or st.get("chime_path") or st.get("alarm_path"))
    try:
        chime_repeat = int(
            data.get("chime_repeat")
            if data.get("chime_repeat") is not None
            else st.get("chime_repeat", 3)
        )
    except Exception:
        chime_repeat = 3
    try:
        speaker_id = int(
            data.get("speaker_id")
            if data.get("speaker_id") is not None
            else st.get("speaker_id", 108)
        )
    except Exception:
        speaker_id = 108
    try:
        tts_repeat = int(
            data.get("tts_repeat")
            if data.get("tts_repeat") is not None
            else st.get("tts_repeat", 3)
        )
    except Exception:
        tts_repeat = 3

    # 実行
    ok = test_preview(
        text=text,
        speaker_id=speaker_id,
        mode=mode,
        chime_path=chime_path,
        chime_repeat=chime_repeat,
        tts_repeat=tts_repeat,
    )
    return jsonify({
        "ok": bool(ok),
        "normalized": {
            "text": text,
            "mode": mode,
            "chime_path": chime_path,
            "chime_repeat": chime_repeat,
            "speaker_id": speaker_id,
            "tts_repeat": tts_repeat,
        }
    })

@timer_bp.route("/api/timer/pending-status", methods=["GET"])
@timer_admin_required
def api_timer_pending_status():
    """
    現在の pending タイマー内容を確認するデバッグ用API。
    例: {"pending": {"label": "2分", "seconds": 120}}
    """
    with _pending_lock:
        pending = _load_pending_timer()
    return jsonify({"pending": pending})

@timer_bp.route("/api/timer/debug_threads")
@timer_admin_required
def api_timer_debug_threads():
    # すでにこのファイルの先頭で
    # from .utils.timer_engine import ... running_threads, ...
    # を import 済みなので、そのまま使える
    from .utils.timer_engine import running_threads
    return jsonify(sorted(list(running_threads.keys())))

# --------------------
# API: last scan 表示用（フロントエンドに生のスキャン文字列を出す）
# --------------------
LAST_SCAN_FILE = "/mnt/mfu/data/last_scan.json"
_last_scan_lock = threading.RLock()

def _load_last_scan() -> dict:
    try:
        with open(LAST_SCAN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_last_scan(data: dict) -> None:
    os.makedirs(os.path.dirname(LAST_SCAN_FILE), exist_ok=True)
    tmp = LAST_SCAN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LAST_SCAN_FILE)

@timer_bp.route("/api/timer/scan-report", methods=["POST"])
@timer_admin_or_loopback_required
def api_timer_scan_report():
    """
    body: { "raw": "4901234567890", "kind": "JAN|MIN|SEC|START|OTHER", "note": "任意" }
    """
    data = request.get_json(force=True, silent=True) or {}
    raw = (data.get("raw") or "").strip()
    kind = (data.get("kind") or "").strip()
    note = (data.get("note") or "").strip()

    if not raw:
        return jsonify({"error": "raw is empty"}), 400

    payload = {
        "raw": raw,
        "kind": kind,
        "note": note,
        "ts": int(time.time()),
    }
    with _last_scan_lock:
        _save_last_scan(payload)
    return jsonify({"status": "ok"})

@timer_bp.route("/api/timer/scan-status", methods=["GET"])
@timer_admin_required
def api_timer_scan_status():
    with _last_scan_lock:
        data = _load_last_scan()
    return jsonify({"last_scan": data})
