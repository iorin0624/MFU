import threading
import time
import subprocess
import json
from datetime import datetime, timedelta
import uuid
import os
import hashlib
import shutil
import logging
from typing import List, Dict, Any, Optional

import io
import wave
import tempfile

# ─────────────────────────────────────────
# 設定
# ─────────────────────────────────────────
TIMER_DATA_FILE = "/mnt/mfu/data/active_timers.json"
ALARM_FILE = "/mnt/mfu/data/scheduled_alarms.json"
TTS_CACHE_DIR = "/mnt/mfu/data/tts_cache"
TIMER_SETTINGS_FILE = "/mnt/mfu/data/timer_settings.json"

# 既存のWAV（フォールバック用）
ALARM_PATH = "/mnt/mfu/app/sound/se_30116.wav"

# VOICEVOX 設定（SE02: ローカル）
VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021")
# ★ デフォルト話者：東北きりたん(108)
SPEAKER_ID = int(os.environ.get("VOICEVOX_SPEAKER", "108"))
USE_VOICEVOX = True  # Falseで従来のWAVのみ

# VOICEVOX通信用タイムアウト・分割・リトライ・バックオフ（環境変数で調整可）
VOICEVOX_CONNECT_TIMEOUT = float(os.environ.get("VOICEVOX_CONNECT_TIMEOUT", "5"))     # 接続
VOICEVOX_QUERY_TIMEOUT   = float(os.environ.get("VOICEVOX_QUERY_TIMEOUT",   "15"))    # audio_query 読み
VOICEVOX_SYNTH_TIMEOUT   = float(os.environ.get("VOICEVOX_SYNTH_TIMEOUT",   "120"))   # synthesis 読み
VOICEVOX_RETRIES         = int(os.environ.get("VOICEVOX_RETRIES", "1"))               # synthesis リトライ回数
TTS_MAX_CHARS            = int(os.environ.get("TTS_MAX_CHARS", "120"))                # これ超で分割モード
TTS_CHUNK_CHARS          = int(os.environ.get("TTS_CHUNK_CHARS", "60"))               # 分割サイズ目安
VOICEVOX_CIRCUIT_BACKOFF_SEC = int(os.environ.get("VOICEVOX_CIRCUIT_BACKOFF_SEC", "60"))  # 連続失敗の一時停止

# ALSA / aplay 設定
APLAY_BIN = os.environ.get("MFU_APLAY") or shutil.which("aplay") or "/usr/bin/aplay"
MFU_ALSA_DEVICE = os.environ.get("MFU_ALSA_DEVICE")  # 例: "mfuquiet" / "plughw:0,0"

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# ユーザー設定の保存・ロード（通知音/回数/話者）
# ─────────────────────────────────────────
_settings_lock = threading.RLock()
_current_settings: Dict[str, Any] = {
    # 新モード："chime_vv"（チャイム→TTS）, "vv_only", "chime_only"
    # 互換のため旧 "sound_mode" も保持（"voicevox"/"wav"）
    "mode": "chime_vv",
    "sound_mode": "voicevox",

    "speaker_id": SPEAKER_ID,
    "tts_repeat": 3,          # TTS（VOICEVOX）回数
    "chime_path": ALARM_PATH, # 指定チャイムパス
    "chime_repeat": 3,        # チャイム回数

    # 旧キー互換（読み書き両対応のため残す）
    "alarm_path": ALARM_PATH,
    "repeat": 3,
}

_settings_mtime = 0.0


def _load_timer_settings_file() -> Dict[str, Any]:
    try:
        with open(TIMER_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_timer_settings_file(d: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(TIMER_SETTINGS_FILE), exist_ok=True)
        with open(TIMER_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _resolve_mode(d: Dict[str, Any]) -> str:
    """新mode優先。未設定なら旧sound_modeをマッピング。最終デフォルト chime_vv。"""
    m = (d.get("mode") or "").lower().strip()
    if m in {"vv_only", "chime_only", "chime_vv"}:
        return m
    sm = (d.get("sound_mode") or "").lower().strip()
    if sm == "voicevox":
        return "vv_only"
    if sm == "wav":
        return "chime_only"
    return "chime_vv"


def _load_timer_settings_on_boot():
    """起動時：ファイル→メモリに読み込み、欠損は既定で補完。互換キーも整合。"""
    global _current_settings, SPEAKER_ID, _settings_mtime
    with _settings_lock:
        fileconf = _load_timer_settings_file()
        if fileconf:
            _current_settings.update(fileconf)

        # 既定補完
        _current_settings.setdefault("speaker_id", SPEAKER_ID)
        _current_settings.setdefault("chime_path", ALARM_PATH)
        _current_settings.setdefault("chime_repeat", 3)
        _current_settings.setdefault("tts_repeat", 3)
        _current_settings.setdefault("sound_mode", "voicevox")

        # 旧キー→新キーの補完
        if "alarm_path" in _current_settings and "chime_path" not in _current_settings:
            _current_settings["chime_path"] = _current_settings["alarm_path"]
        if "repeat" in _current_settings:
            _current_settings.setdefault("chime_repeat", int(_current_settings["repeat"]))
            _current_settings.setdefault("tts_repeat", int(_current_settings["repeat"]))

        # mode 正規化
        _current_settings["mode"] = _resolve_mode(_current_settings)

        # 実ランタイムへ話者のみ反映
        try:
            SPEAKER_ID = int(_current_settings.get("speaker_id", SPEAKER_ID))
        except Exception:
            pass

        try:
            _settings_mtime = os.path.getmtime(TIMER_SETTINGS_FILE)
        except Exception:
            _settings_mtime = 0.0


def _reload_settings_if_changed():
    """TIMER_SETTINGS_FILE が更新されていたら _current_settings に反映（全プロセス同期）"""
    global _settings_mtime, _current_settings, SPEAKER_ID
    try:
        mt = os.path.getmtime(TIMER_SETTINGS_FILE)
    except Exception:
        mt = 0.0
    if mt <= _settings_mtime:
        return
    with _settings_lock:
        try:
            fileconf = _load_timer_settings_file()
            if fileconf:
                s = dict(_current_settings)
                s.update(fileconf)

                # 既定補完＆互換吸収
                s.setdefault("speaker_id", SPEAKER_ID)
                s.setdefault("chime_path", ALARM_PATH)
                s.setdefault("chime_repeat", 3)
                s.setdefault("tts_repeat", 3)
                if "alarm_path" in s and "chime_path" not in s:
                    s["chime_path"] = s["alarm_path"]
                if "repeat" in s:
                    s.setdefault("chime_repeat", int(s["repeat"]))
                    s.setdefault("tts_repeat", int(s["repeat"]))

                s["mode"]       = _resolve_mode(s)
                s["sound_mode"] = s.get("sound_mode", "voicevox")
                s["repeat"]     = max(int(s.get("tts_repeat", 3)), int(s.get("chime_repeat", 3)))
                s["alarm_path"] = s.get("chime_path", s.get("alarm_path", ALARM_PATH))

                _current_settings = s
                try:
                    SPEAKER_ID = int(s.get("speaker_id", SPEAKER_ID))
                except Exception:
                    pass
            _settings_mtime = mt
        except Exception as e:
            logger.warning("settings reload failed: %s", e)


def get_timer_settings() -> Dict[str, Any]:
    with _settings_lock:
        # 互換性のため旧キーも含めて返す
        s = dict(_current_settings)
        s["alarm_path"] = s.get("chime_path", s.get("alarm_path", ALARM_PATH))
        s["repeat"] = max(int(s.get("tts_repeat", 3)), int(s.get("chime_repeat", 3)))
        s["sound_mode"] = s.get("sound_mode", "voicevox")
        s["mode"] = _resolve_mode(s)
        return s


def set_timer_settings(new_settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    正式キー:
      - mode: "chime_vv" / "vv_only" / "chime_only"
      - speaker_id: int
      - tts_repeat: int (1..20)
      - chime_path: str
      - chime_repeat: int (1..20)
    互換キー（来たら吸収して反映）:
      - sound_mode: "voicevox"→vv_only, "wav"→chime_only
      - alarm_path -> chime_path
      - repeat -> tts_repeat/chime_repeat（個別指定が優先）
    """
    global _current_settings, SPEAKER_ID
    with _settings_lock:
        s = dict(_current_settings)

        # ── 別名吸収（互換） ──
        alias: Dict[str, Any] = {}
        if "sound_mode" in new_settings:
            sm = str(new_settings["sound_mode"]).lower().strip()
            alias["sound_mode"] = sm
            if sm == "voicevox":
                alias["mode"] = "vv_only"
            elif sm == "wav":
                alias["mode"] = "chime_only"
        if "alarm_path" in new_settings and "chime_path" not in new_settings:
            alias["chime_path"] = new_settings["alarm_path"]
        if "repeat" in new_settings:
            if "tts_repeat" not in new_settings:
                alias["tts_repeat"] = new_settings["repeat"]
            if "chime_repeat" not in new_settings:
                alias["chime_repeat"] = new_settings["repeat"]

        data = dict(new_settings)
        data.update(alias)

        # ── 正式フィールド反映 ──
        if "mode" in data:
            mode = str(data["mode"]).lower().strip()
            if mode in {"vv_only", "chime_only", "chime_vv"}:
                s["mode"] = mode

        if "speaker_id" in data:
            try:
                s["speaker_id"] = int(data["speaker_id"])
            except Exception:
                pass

        if "tts_repeat" in data:
            try:
                s["tts_repeat"] = max(1, min(20, int(data["tts_repeat"])))
            except Exception:
                pass

        if "chime_path" in data:
            ap = str(data["chime_path"]).strip()
            if ap:
                s["chime_path"] = ap
                s["alarm_path"] = ap  # 互換も更新

        if "chime_repeat" in data:
            try:
                s["chime_repeat"] = max(1, min(20, int(data["chime_repeat"])))
            except Exception:
                pass

        # 互換フィールドも同期
        s["sound_mode"] = s.get("sound_mode", "voicevox")
        s["mode"] = _resolve_mode(s)
        s["repeat"] = max(int(s.get("tts_repeat", 3)), int(s.get("chime_repeat", 3)))
        s["alarm_path"] = s.get("chime_path", s.get("alarm_path", ALARM_PATH))

        # 保存
        if _save_timer_settings_file(s):
            _current_settings = s
            # ランタイムへ話者のみ即反映
            try:
                SPEAKER_ID = int(s.get("speaker_id", SPEAKER_ID))
            except Exception:
                pass

        return dict(_current_settings)


# ディレクトリ確保
os.makedirs(os.path.dirname(ALARM_FILE), exist_ok=True)
os.makedirs(os.path.dirname(TIMER_DATA_FILE), exist_ok=True)
os.makedirs(TTS_CACHE_DIR, exist_ok=True)

# グローバル変数
running_threads: Dict[str, threading.Thread] = {}  # label -> Thread（タイマー用）
_scheduled_lock = threading.RLock()
scheduled_alarms: List[Dict[str, Any]] = []  # メモリ上の最新リスト

# グローバル変数
running_threads: Dict[str, threading.Thread] = {}  # label -> Thread（タイマー用）
# 追加：キャンセルフラグ
_timer_cancel_flags: Dict[str, threading.Event] = {}  # label -> Event

_scheduled_lock = threading.RLock()
scheduled_alarms: List[Dict[str, Any]] = []  # メモリ上の最新リスト


# ─────────────────────────────────────────
# 内部ユーティリティ（タイマー）
# ─────────────────────────────────────────
def _save_active_timers(data: Dict[str, Any]) -> None:
    """
    active_timers.json を安全に保存する。

    - 同一ディレクトリに一意な一時ファイルを作成してから os.replace で入れ替え
      （gthread + 複数ワーカーの同時呼び出しでも競合しないようにする）
    """
    base_dir = os.path.dirname(TIMER_DATA_FILE)
    os.makedirs(base_dir, exist_ok=True)

    fd = None
    tmp_path: Optional[str] = None

    try:
        # 一意な tmp ファイルを作成（同時呼び出しでも衝突しない）
        fd, tmp_path = tempfile.mkstemp(
            dir=base_dir,
            prefix="active_timers.",
            suffix=".tmp",
        )

        # mkstemp は fd を返すので、fdopen でテキストモードにする
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        # アトミックに本番ファイルと入れ替え
        os.replace(tmp_path, TIMER_DATA_FILE)
        tmp_path = None  # 正常終了したので削除不要
    except Exception as e:
        # 途中まで作った tmp が残っていたら掃除
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        logger.error("active_timers.json の保存に失敗しました: %s", e)


def _load_active_timers() -> Dict[str, Any]:
    try:
        with open(TIMER_DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _remove_timer(label: str):
    timers = _load_active_timers()
    if label in timers:
        del timers[label]
        _save_active_timers(timers)
    if label in running_threads:
        del running_threads[label]
    # 追加：キャンセルフラグも掃除
    _timer_cancel_flags.pop(label, None)


# ─────────────────────────────────────────
# ALSA 再生（PipeWire不在でも確実に出す）
# ─────────────────────────────────────────
def _has_pcm(name: str) -> bool:
    try:
        out = subprocess.check_output([APLAY_BIN, "-L"], text=True, timeout=2)
        return any(line.strip() == name for line in out.splitlines())
    except Exception:
        return False


def _choose_alsa_dev() -> str:
    # 環境変数があれば最優先
    if MFU_ALSA_DEVICE:
        return MFU_ALSA_DEVICE
    # softvol があればそれを使う
    if _has_pcm("mfuquiet"):
        return "mfuquiet"
    # 内蔵優先、次いでHDMI
    return "plughw:0,0"


def _aplay(path: str, alsa_dev: Optional[str] = None) -> bool:
    dev = alsa_dev or _choose_alsa_dev()
    candidates = [dev]
    for alt in ("plughw:0,0", "plughw:0,5", "plughw:0,6", "plughw:0,7"):
        if alt not in candidates:
            candidates.append(alt)

    last_err = None
    for d in candidates:
        try:
            subprocess.run([APLAY_BIN, "-q", "-D", d, path], check=True, timeout=30)
            return True
        except Exception as e:
            last_err = e
            logger.warning("aplay失敗(dev=%s): %s", d, e)
            continue
    logger.error("aplay失敗: %s", last_err)
    return False


# ─────────────────────────────────────────
# VOICEVOX: セッション/分割合成/結合/キャッシュ
# ─────────────────────────────────────────
_requests_session = None
_vv_circuit_open_until = 0.0  # epoch 秒。ここまでVOICEVOX呼び出しを抑止


def _now() -> float:
    return time.time()


def _vv_session():
    """requests.Session を返す。失敗時は再生成。"""
    global _requests_session
    if _requests_session is None:
        import requests
        s = requests.Session()
        _requests_session = s
    return _requests_session


def _vv_session_reset():
    global _requests_session
    try:
        if _requests_session is not None:
            _requests_session.close()
    except Exception:
        pass
    _requests_session = None


def _open_circuit():
    """連続失敗時の一時停止（サーキットブレーカー）。"""
    global _vv_circuit_open_until
    _vv_circuit_open_until = _now() + VOICEVOX_CIRCUIT_BACKOFF_SEC
    logger.warning("[VOICEVOX] circuit open for %ds", VOICEVOX_CIRCUIT_BACKOFF_SEC)


def _circuit_allows() -> bool:
    return _now() >= _vv_circuit_open_until


def _split_text_for_tts(text: str, max_len: int = TTS_CHUNK_CHARS) -> List[str]:
    """句読点・改行でこまめに分割。残りが長ければ強制カット。"""
    t = (text or "").strip()
    if not t:
        return []
    if len(t) <= max_len:
        return [t]

    seps = "。！？\n\r"
    parts: List[str] = []
    buf = ""

    def flush():
        nonlocal buf
        b = buf.strip()
        if b:
            parts.append(b)
        buf = ""

    for ch in t:
        buf += ch
        if ch in seps and len(buf) >= max_len // 2:
            flush()
        elif len(buf) >= max_len:
            flush()
    flush()

    # 念のため過大チャンクをさらに割る
    final: List[str] = []
    for p in parts:
        if len(p) <= max_len:
            final.append(p)
        else:
            for i in range(0, len(p), max_len):
                final.append(p[i:i + max_len])
    return final


def _looks_like_wav(data: bytes) -> bool:
    return len(data) >= 44 and data[:4] == b"RIFF"


def _voicevox_initialize_speaker(speaker_id: int):
    if not USE_VOICEVOX or not _circuit_allows():
        return
    try:
        s = _vv_session()
        resp = s.post(
            f"{VOICEVOX_URL}/initialize_speaker",
            params={"speaker": speaker_id},
            timeout=(VOICEVOX_CONNECT_TIMEOUT, 5),
        )
        if resp.status_code == 200:
            logger.info("[VOICEVOX] speaker %s initialized", speaker_id)
    except Exception as e:
        logger.warning("[VOICEVOX] initialize_speaker 失敗: %s", e)
        _vv_session_reset()


def _vv_audio_query(s, text: str, speaker_id: int) -> Dict[str, Any]:
    r = s.post(
        f"{VOICEVOX_URL}/audio_query",
        params={"text": text, "speaker": speaker_id},
        timeout=(VOICEVOX_CONNECT_TIMEOUT, VOICEVOX_QUERY_TIMEOUT),
        json={},
    )
    r.raise_for_status()
    qj = r.json()
    # 安全な範囲で速度を軽く調整
    qj["speedScale"] = min(1.2, max(0.9, qj.get("speedScale", 1.0)))
    return qj


def _vv_synthesis(s, query_json: Dict[str, Any], speaker_id: int) -> bytes:
    last_e = None
    for attempt in range(VOICEVOX_RETRIES + 1):
        try:
            r = s.post(
                f"{VOICEVOX_URL}/synthesis",
                params={"speaker": speaker_id, "enable_interrogative_upspeak": True},
                json=query_json,
                timeout=(VOICEVOX_CONNECT_TIMEOUT, VOICEVOX_SYNTH_TIMEOUT),
            )
            r.raise_for_status()
            return r.content
        except Exception as e:
            last_e = e
            logger.warning(
                "[VOICEVOX] synthesis retry %d/%d: %s",
                attempt + 1,
                VOICEVOX_RETRIES,
                e,
            )
            _vv_session_reset()
            if attempt >= VOICEVOX_RETRIES:
                break
    raise last_e if last_e else RuntimeError("VOICEVOX synthesis failed")


def _concat_wavs(wavs: List[bytes]) -> bytes:
    """複数WAV(同一fmt想定)を連結し1本へ。"""
    if not wavs:
        return b""
    pcm_parts: List[bytes] = []
    fmt_params = None  # (nchannels, sampwidth, framerate, comptype, compname)
    for data in wavs:
        if not _looks_like_wav(data):
            continue
        with wave.open(io.BytesIO(data), "rb") as w:
            params = (
                w.getnchannels(),
                w.getsampwidth(),
                w.getframerate(),
                w.getcomptype(),
                w.getcompname(),
            )
            frames = w.readframes(w.getnframes())
            if fmt_params is None:
                fmt_params = params
            else:
                if params != fmt_params:
                    logger.warning(
                        "[VOICEVOX] 異なるWAVフォーマットは連結不可: %s != %s",
                        params,
                        fmt_params,
                    )
                    continue
            pcm_parts.append(frames)

    if not pcm_parts or fmt_params is None:
        return b""

    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        nch, sw, fr, ct, cn = fmt_params
        w.setnchannels(nch)
        w.setsampwidth(sw)
        w.setframerate(fr)
        w.setcomptype(ct, cn)
        for pcm in pcm_parts:
            w.writeframes(pcm)
    return out.getvalue()


def _tts_bytes(text: str, speaker_id: int) -> Optional[bytes]:
    """テキスト→（分割あり）→WAVバイト列。失敗時None。サーキット開の場合は即None。"""
    if not _circuit_allows():
        return None
    try:
        s = _vv_session()
        chunks = (
            _split_text_for_tts(text, TTS_CHUNK_CHARS)
            if len(text) > TTS_MAX_CHARS
            else [text]
        )
        wav_bytes_list: List[bytes] = []
        for ch in chunks:
            qj = _vv_audio_query(s, ch, speaker_id)
            data = _vv_synthesis(s, qj, speaker_id)
            if not _looks_like_wav(data):
                logger.error("[VOICEVOX] synthesis 非WAV応答（分割）: %s", data[:200])
                return None
            wav_bytes_list.append(data)
        return _concat_wavs(wav_bytes_list) if len(wav_bytes_list) > 1 else wav_bytes_list[0]
    except Exception as e:
        logger.error("[VOICEVOX] 合成失敗: %s", e)
        _open_circuit()          # 一定時間は以降の呼び出しを抑止
        return None


def _cache_key(speaker_id: int, text: str) -> str:
    h = hashlib.sha256()
    h.update(f"{speaker_id}\n{text}".encode("utf-8"))
    return h.hexdigest()


def _tts_cache_path(speaker_id: int, text: str) -> str:
    return os.path.join(TTS_CACHE_DIR, _cache_key(speaker_id, text) + ".wav")


def _tts_ensure_cached_wav(text: str, speaker_id: Optional[int] = None) -> Optional[str]:
    if not USE_VOICEVOX:
        return None
    sid = int(speaker_id if speaker_id is not None else SPEAKER_ID)
    path = _tts_cache_path(sid, text)
    if os.path.exists(path) and os.path.getsize(path) > 44:
        return path

    data = _tts_bytes(text, sid)
    if not data:
        return None
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return path
    except Exception as e:
        logger.error("[VOICEVOX] キャッシュ保存失敗: %s", e)
        return None


# ─────────────────────────────────────────
# アラーム音（組み合わせ／※vv_onlyはフォールバック無し）
# ─────────────────────────────────────────
def _play_alarm(label_for_tts: str = "アラーム"):
    # 毎回、現在設定を読み込んで適用
    with _settings_lock:
        mode         = _resolve_mode(_current_settings)
        speaker      = int(_current_settings.get("speaker_id", SPEAKER_ID))
        tts_repeat   = int(_current_settings.get("tts_repeat", 3))
        chime_path   = _current_settings.get("chime_path", ALARM_PATH)
        chime_repeat = int(_current_settings.get("chime_repeat", 3))

    # TTS 事前用意
    message = f"{label_for_tts} の時間です。"
    cached = None
    if mode in ("vv_only", "chime_vv") and USE_VOICEVOX:
        cached = _tts_ensure_cached_wav(message, speaker)

    def _beep(n: int):
        for _ in range(max(1, n)):
            _aplay(chime_path)
            time.sleep(0.2)

    def _speak(n: int, allow_fallback: bool):
        for _ in range(max(1, n)):
            played = False
            if cached:
                played = _aplay(cached)
            if not played and allow_fallback:
                _aplay(chime_path)
            time.sleep(0.2)

    if mode == "vv_only":
        # フォールバックなし（失敗時は沈黙）
        _speak(tts_repeat, allow_fallback=False)
    elif mode == "chime_only":
        _beep(chime_repeat)
    else:  # "chime_vv" = チャイム → 読み上げ（TTS失敗時も再フォールバックなし：先に鳴っているため）
        _beep(chime_repeat)
        _speak(tts_repeat, allow_fallback=False)


def _calc_chime_lead_seconds() -> float:
    """
    チャイム+TTS(chime_vv) のときだけ、
    「チャイムが全部鳴き終わるまでの秒数」を返す。
    それ以外のモードでは 0.0 を返す。
    """
    try:
        with _settings_lock:
            mode = _resolve_mode(_current_settings)
            chime_path = _current_settings.get("chime_path", ALARM_PATH)
            chime_repeat = int(_current_settings.get("chime_repeat", 3))

        # チャイム+TTS 以外は前倒し不要
        if mode != "chime_vv":
            return 0.0

        if not chime_path or not os.path.exists(chime_path):
            return 0.0

        # WAV から長さを取得
        with wave.open(chime_path, "rb") as wf:
            frames = wf.getnframes()
            fr = wf.getframerate() or 1

        single = frames / float(fr)
        if single <= 0:
            return 0.0

        # _beep() 内の time.sleep(0.2) を考慮して、1回分 = 音長 + 0.2s
        gap = 0.2
        total = (single + gap) * max(1, chime_repeat)

        return max(0.0, total)
    except Exception as e:
        logger.debug("[ALARM] チャイム長計算失敗: %s", e)
        return 0.0


# ─────────────────────────────────────────
# タイマー関連
# ─────────────────────────────────────────
def start_timer(seconds: int, label: str):
    """
    seconds は「TTS が鳴り始めるまでの時間」として扱う。

    - mode == "chime_vv":
        指定秒数 (=TTS開始) からチャイム長を引いた時刻に _play_alarm を開始
    - mode == "vv_only" / "chime_only":
        今まで通り、指定秒数後に _play_alarm を開始
    """
    # ユーザーに見せる「終了予定時刻」は TTS 開始時刻ベース
    tts_target_dt = datetime.now() + timedelta(seconds=seconds)
    ends_at = tts_target_dt.strftime("%Y-%m-%d %H:%M:%S")

    def _prefetch():
        # モード確認して、必要ならTTSプリウォーム（失敗しても落とさない）
        try:
            with _settings_lock:
                mode = _resolve_mode(_current_settings)
                spk  = int(_current_settings.get("speaker_id", SPEAKER_ID))
            if mode in ("vv_only", "chime_vv"):
                _voicevox_initialize_speaker(spk)
                _tts_ensure_cached_wav(f"{label} の時間です。", spk)
        except Exception as e:
            logger.debug("prefetch error: %s", e)

    threading.Thread(target=_prefetch, daemon=True).start()

    # チャイム+TTS のときだけ、チャイム分だけ前倒しする
    lead_sec = _calc_chime_lead_seconds()
    sleep_for = max(0.0, seconds - lead_sec)

    # 追加：キャンセルフラグ
    cancel_evt = threading.Event()

    def timer_thread():
        # 細かく刻んでキャンセルに応答できるようにする
        remaining = sleep_for
        while remaining > 0:
            if cancel_evt.is_set():
                logger.info("[TIMER] 🔕 キャンセル済み（スレッド終了）: %s", label)
                return
            step = min(1.0, remaining)  # 1秒刻みでチェック
            time.sleep(step)
            remaining -= step

        # 最後にもう一度チェック
        if cancel_evt.is_set():
            logger.info("[TIMER] 🔕 キャンセル済み（スレッド終了）: %s", label)
            return

        logger.info("[TIMER] 🔔 終了: %s", label)
        _play_alarm(label_for_tts=label)
        _remove_timer(label)

    t = threading.Thread(target=timer_thread, daemon=True)
    t.start()

    running_threads[label] = t
    _timer_cancel_flags[label] = cancel_evt  # 追加：キャンセルフラグも保存

    timers = _load_active_timers()
    timers[label] = ends_at  # 表示用は「TTS が鳴る時刻」
    _save_active_timers(timers)


def get_active_timers():
    """
    /api/timer/status 用。
    - 期限切れタイマーは内部辞書から掃除
    - 未来のものだけを返す
    """
    now = datetime.now()
    timers = _load_active_timers()
    result = []
    updated = {}

    for label, end_str in timers.items():
        try:
            ends_at = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
            remaining = ends_at - now
            if remaining.total_seconds() > 0:
                result.append(
                    {
                        "label": label,
                        "ends_at": ends_at.strftime("%H:%M:%S"),
                        "remaining": str(remaining).split(".")[0],
                    }
                )
                updated[label] = end_str
        except Exception:
            continue

    _save_active_timers(updated)
    # 時刻順にソート
    result.sort(key=lambda x: x["ends_at"])
    return result

def cancel_timer(label: str):
    # 先にキャンセルフラグを立ててスレッドに終了してもらう
    evt = _timer_cancel_flags.get(label)
    if evt:
        evt.set()
    _remove_timer(label)
    logger.info("[TIMER] ❌ キャンセル: %s", label)


# ─────────────────────────────────────────
# アラーム（永続化+スレッド復元+掃除）
# ─────────────────────────────────────────
def _read_alarm_file() -> List[Dict[str, Any]]:
    try:
        with open(ALARM_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _write_alarm_file(items: List[Dict[str, Any]]):
    tmp = ALARM_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ALARM_FILE)


def _prune_and_save(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    now_ts = time.time()
    alive = [a for a in items if a.get("timestamp", 0) > now_ts]
    if len(alive) != len(items):
        _write_alarm_file(alive)
    return alive


def _alarm_thread(target_ts, label, alarm_id):
    # 待機（細かく刻んでシグナル対応）
    while True:
        now = time.time()
        remain = target_ts - now
        if remain <= 0:
            break
        time.sleep(min(1, remain))

    logger.info("[ALARM] 🔔 アラーム発動: %s", label)
    _play_alarm(label_for_tts=label)

    # 発火後は一覧から削除
    with _scheduled_lock:
        global scheduled_alarms
        scheduled_alarms = [a for a in scheduled_alarms if a.get("id") != alarm_id]
        _write_alarm_file(scheduled_alarms)


def set_alarm(hour, minute, label="アラーム"):
    """
    hour / minute は「TTS が鳴り始める時刻」として扱う。

    - chime_vv: 指定時刻からチャイム長だけ前倒しして _alarm_thread を開始
    - vv_only / chime_only: 指定時刻ちょうどに _alarm_thread 開始（従来通り）
    """
    now = datetime.now()

    # 指定された「TTS 開始時刻」を求める
    tts_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tts_time < now:
        tts_time += timedelta(days=1)

    # チャイム+TTS のときだけ、チャイム分だけ前倒し
    lead_sec = _calc_chime_lead_seconds()
    target_ts = tts_time.timestamp() - lead_sec

    # すでに過去になってしまう場合は、今すぐ開始にクリップ
    now_ts = time.time()
    if target_ts < now_ts:
        target_ts = now_ts

    alarm_id = str(uuid.uuid4())

    # 事前合成（必要な場合のみ）
    with _settings_lock:
        mode = _resolve_mode(_current_settings)
        spk  = int(_current_settings.get("speaker_id", SPEAKER_ID))
    if mode in ("vv_only", "chime_vv"):
        threading.Thread(
            target=_tts_ensure_cached_wav,
            args=(f"{label} の時間です。", spk),
            daemon=True,
        ).start()

    entry = {
        "id": alarm_id,
        "label": label,
        "hour": hour,          # 表示用：ユーザーが指定した「TTS開始時刻」
        "minute": minute,
        "timestamp": target_ts # 実際に _alarm_thread が動き出す時刻（チャイム開始）
    }

    with _scheduled_lock:
        global scheduled_alarms
        current = _prune_and_save(_read_alarm_file())
        current.append(entry)
        _write_alarm_file(current)
        scheduled_alarms = current

    t = threading.Thread(target=_alarm_thread, args=(target_ts, label, alarm_id), daemon=True)
    t.start()

    logger.debug("[ALARM] 登録: %s", entry)
    return alarm_id


def get_scheduled_alarms():
    with _scheduled_lock:
        global scheduled_alarms
        scheduled_alarms = _prune_and_save(_read_alarm_file())
        return sorted(scheduled_alarms, key=lambda x: x["timestamp"])


def cancel_alarm(label: str) -> bool:
    with _scheduled_lock:
        global scheduled_alarms
        items = _read_alarm_file()
        before = len(items)
        items = [a for a in items if a.get("label") != label]
        if len(items) < before:
            _write_alarm_file(items)
            scheduled_alarms = items
            logger.info("[ALARM] ❌ キャンセル: %s", label)
            return True
        return False


def load_alarms():
    """起動時に呼ばれ、未来のアラームのみスレッド再起動＆期限切れ掃除"""
    with _scheduled_lock:
        global scheduled_alarms
        items = _prune_and_save(_read_alarm_file())
        scheduled_alarms = items

        now_ts = time.time()
        for a in items:
            ts = a.get("timestamp")
            if ts and ts > now_ts:
                threading.Thread(
                    target=_alarm_thread, args=(ts, a.get("label", "アラーム"), a.get("id")), daemon=True
                ).start()


def save_alarms():
    # 互換用（外部から呼ばれても原子的保存）
    with _scheduled_lock:
        _write_alarm_file(scheduled_alarms)


# 起動時にプリウォーム（話者モデル読み込み）
def _warmup_on_boot():
    _load_timer_settings_on_boot()
    try:
        with _settings_lock:
            mode = _resolve_mode(_current_settings)
            spk  = int(_current_settings.get("speaker_id", SPEAKER_ID))
        if mode in ("vv_only", "chime_vv"):
            _voicevox_initialize_speaker(spk)
    except Exception:
        pass


threading.Thread(target=_warmup_on_boot, daemon=True).start()

# 起動時にアラームを復元
load_alarms()


# ─────────────────────────────────────────
# VOICEVOX 補助（話者一覧・テスト）
# ─────────────────────────────────────────
def list_voicevox_speakers() -> List[Dict[str, Any]]:
    if not _circuit_allows():
        # バックオフ中は最小情報だけ返す
        return [
            {
                "name": "Default",
                "styles": [
                    {
                        "name": "normal",
                        "id": _current_settings.get("speaker_id", SPEAKER_ID),
                    }
                ],
            }
        ]
    try:
        s = _vv_session()
        res = s.get(f"{VOICEVOX_URL}/speakers", timeout=(VOICEVOX_CONNECT_TIMEOUT, 5))
        res.raise_for_status()
        return res.json()
    except Exception:
        _vv_session_reset()
        # 最低限のデフォルト
        return [
            {
                "name": "Default",
                "styles": [
                    {
                        "name": "normal",
                        "id": _current_settings.get("speaker_id", SPEAKER_ID),
                    }
                ],
            }
        ]


def test_speak_once(text: str, speaker_id: int) -> bool:
    """従来互換：TTSを1回だけ生成→再生（失敗時は無音。フォールバックしない）"""
    try:
        path = _tts_ensure_cached_wav(text, int(speaker_id))
        if not path:
            return False
        return _aplay(path)
    except Exception:
        return False


def test_preview(
    text: str,
    speaker_id: int,
    mode: str,
    chime_path: str,
    chime_repeat: int,
    tts_repeat: int,
) -> bool:
    """
    指定テキストでプレビュー再生。
    mode: "vv_only" | "chime_only" | "chime_vv"
    ※ テストは安全のため各回数は最大5に丸める
    """
    mode = (mode or "chime_vv").lower().strip()
    chime_repeat = max(1, min(5, int(chime_repeat)))
    tts_repeat = max(1, min(5, int(tts_repeat)))
    ok_all = True

    cached = None
    if mode in ("vv_only", "chime_vv") and USE_VOICEVOX:
        cached = _tts_ensure_cached_wav((text or "テストです。").strip(), int(speaker_id))

    def _beep(n: int):
        nonlocal ok_all
        for _ in range(n):
            ok_all = _aplay(chime_path) and ok_all
            time.sleep(0.2)

    def _speak(n: int, allow_fallback: bool):
        nonlocal ok_all
        for _ in range(n):
            played = False
            if cached:
                played = _aplay(cached)
            if not played and allow_fallback:
                ok_all = _aplay(chime_path) and ok_all
            time.sleep(0.2)

    if mode == "vv_only":
        _speak(tts_repeat, allow_fallback=False)  # フォールバックなし
    elif mode == "chime_only":
        _beep(chime_repeat)
    else:  # chime_vv
        _beep(chime_repeat)
        _speak(tts_repeat, allow_fallback=False)  # チャイムは先に鳴っているので再フォールバックしない

    return ok_all
