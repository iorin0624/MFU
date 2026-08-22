# app/utils/logs.py
from __future__ import annotations

import time
import ipaddress
import threading
import os
import json
from datetime import datetime
from typing import Optional, Dict, Tuple, List, Union
from flask import g, request as _req, current_app
from app.utils.db import get_db
from app.utils.fw_auto_ban import choose_ban_escalation, evaluate_events, enforcement_enabled
from app.utils.fw_ban import (
    permanently_ban_ip_cidr_via_ssh,
    temporarily_ban_ip_cidr_via_ssh,
    unban_auto_permanent_ip_cidr_via_ssh,
)
from app.utils.whois_util import get_netinfo

# 外部: Discord 通知用（無ければ黙ってスキップ）
try:
    import requests
except Exception:
    requests = None

# --- debug helpers (runtime switchable) ------------------------------
import sys, json
from urllib import request as _urlreq
from urllib.error import URLError, HTTPError

# truthy 文字列判定（1/true/on/yes → True）
def _truthy(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "on", "yes", "y", "t")

# ランタイムで有効/無効を判定する
# 優先度: Flask config → フラグファイル → 環境変数 → 既定OFF
_DEBUG_FLAG_FILES = [
    "/mnt/mfu/logs.debug",          # 空ファイルでも“有効”とみなす
    "/mnt/mfu/app/logs.debug",
    "/etc/mfu/logs.debug",
]

def _is_debug_enabled() -> bool:
    # 1) Flask設定（最優先）
    try:
        from flask import current_app
        if current_app:
            if "MFU_LOGS_DEBUG" in current_app.config:
                return _truthy(current_app.config.get("MFU_LOGS_DEBUG"))
            # 別名も一応見る
            for alt in ("LOGS_DEBUG", "DEBUG_LOGS"):
                if alt in current_app.config:
                    return _truthy(current_app.config.get(alt))
    except Exception:
        pass

    # 2) フラグファイル（存在すればON / 中身が書いてあれば truthy で判定）
    for p in _DEBUG_FLAG_FILES:
        try:
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        content = f.read().strip()
                    return True if content == "" else _truthy(content)
                except Exception:
                    # 読めなくても存在していればON扱い
                    return True
        except Exception:
            pass

    # 3) 環境変数（未設定時は既定OFF）
    env = os.getenv("MFU_LOGS_DEBUG", "0")
    return _truthy(env)

def _dbg(msg: str) -> None:
    """サーバーターミナルに確実に出す簡易デバッグ出力（ランタイム切替対応）"""
    if not _is_debug_enabled():
        return
    line = f"[logs.py DEBUG] {msg}\n"
    try:
        sys.stderr.write(line); sys.stderr.flush()
    except Exception:
        pass
    try:
        # INFO/DEBUGだとprodで出ない事があるため WARNING に寄せる
        from flask import current_app
        current_app.logger.warning(line.strip())
    except Exception:
        pass

# 外部から切替したいとき用（任意で使用可）:
def set_debug_logs(enabled: bool) -> None:
    """
    Flaskアプリ設定にフラグを書き込む。以後は再起動なしで反映。
    例: from app.utils.logs import set_debug_logs; set_debug_logs(True)
    """
    try:
        from flask import current_app
        if current_app:
            current_app.config["MFU_LOGS_DEBUG"] = bool(enabled)
    except Exception:
        pass

def toggle_debug_logs() -> None:
    """現在値を反転（Flask設定に保存）"""
    try:
        from flask import current_app
        if current_app:
            cur = _truthy(current_app.config.get("MFU_LOGS_DEBUG", None))
            current_app.config["MFU_LOGS_DEBUG"] = (not cur)
    except Exception:
        pass
# --------------------------------------------------------------------

# ==========================================================
# 記録対象外の定数リスト（ここだけ編集すればOK）
# ==========================================================
SKIP_PREFIXES = [
    "/static/",
    "/image_viewer/thumbs/",  # サムネイル配信は大量発生するためDBへ記録しない
    "/image_viewer/files/",   # 画像・動画本体の配信もDBへ記録しない
    "/api/ext/up/",       # 外部アップロードAPI配下は全部除外（/api/ext/up/thumb, /original等）
    "/tickets/thumb/",    # 追加: チケットのサムネ表示を除外
    "/tickets/preview/",
    "/tickets/api/status/",
    "/tickets/dl/",
    "/tickets/api/zip/",
    "/tickets/api/files/",
    "/admin/nodes/data",
    "/api/timer/status",
    "/apple-touch-icon",  # ← これを追加（*.png / *-120x120 など全部まとめて対象）
]

SKIP_PATHS = [
    "/favicon.ico",
    "/robots.txt",
    "/healthz",
    "/api/cpu_usage",
    "/api_temp_sensor",
    "/api_vcgencmd",
    "/api/zip-progress",  # ZIP進捗ポーリングを除外
]

SKIP_ENDPOINTS = {
    "static",
    "api_cpu_usage",
    "temp_sensor",
    "api_vcgencmd",
}

# ----------------------------------------------------------
# 補助：バイナリ配信など（旧来の除外条件を継続）
# ----------------------------------------------------------
def _is_binary_media_path(p: str) -> bool:
    """
    画像等のバイナリ配信パスを厳密に除外判定。
    """
    if not p:
        return False
    parts = [s for s in p.split("/") if s]
    if len(parts) < 2:
        return False

    # 既存: アルバム画像サムネ/本体
    if parts[0] == "album" and len(parts) >= 4 and parts[2] in {"thumb", "image"}:
        return True

    # ★追加: アルバム動画（ダウンロード／raw／ポスター）も除外
    # 例:
    #   /album/<album_uuid>/movie/download/<movie_uuid>/<filename>.mov
    #   /album/<album_uuid>/movie/raw/<movie_uuid>/<filename>.web.mp4
    #   /album/<album_uuid>/movie/poster/<movie_uuid>/<filename>.poster.jpg
    if parts[0] == "album" and len(parts) >= 4 and parts[2] == "movie" and parts[3] in {"download", "raw", "poster"}:
        return True

    # 既存: アップロード画像
    if parts[0] == "uploads" and len(parts) >= 3 and parts[2] in {"thumb", "original"}:
        return True

    # 既存: 外部ログインのアバター
    if parts[0] == "external-login" and len(parts) >= 2 and parts[1] == "avatars":
        return True

    return False


def should_skip_access_log(path: str, endpoint: Optional[str]) -> bool:
    """アクセスログ対象外の条件をまとめて判定。"""
    base_endpoint = (endpoint or "").split(".")[-1] if endpoint else ""
    if any(path.startswith(p) for p in SKIP_PREFIXES):
        _dbg(f"skip-log(reason=prefix) path={path}")
        return True
    if path in SKIP_PATHS:
        _dbg(f"skip-log(reason=path) path={path}")
        return True
    if base_endpoint in SKIP_ENDPOINTS:
        _dbg(f"skip-log(reason=endpoint) path={path} endpoint={base_endpoint}")
        return True
    if _is_binary_media_path(path):
        _dbg(f"skip-log(reason=binary) path={path}")
        return True
    return False


# ==========================================================
# 文字列丸め（DBの列長に安全寄せ）
# ==========================================================
def _clamp(s: Union[str, None], n: int) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n]


# ==========================================================
# 基本のINSERT（互換維持：log_request_raw を公開）
# ==========================================================
def log_request_raw(
    *,
    ip,
    method,
    path,
    status,
    ua,
    referer,
    endpoint,
    username,
    latency_ms,
    location=None,   # ← 追加
    marker=None,
) -> None:
    """
    旧来互換の生INSERT関数。
    logsテーブルにカラム一式＋log_textを保存。
    """
    # 文字列はDB安全側に丸める（列長は環境に応じて調整）
    ip         = _clamp(ip or "-", 64)
    method     = _clamp(method or "-", 16)
    path       = _clamp(path or "-", 512)
    ua         = _clamp(ua or "-", 512)
    referer    = _clamp(referer or "", 1024)
    endpoint   = _clamp(endpoint or "", 128)
    username   = _clamp(username or "", 128)
    status     = int(status) if status is not None else 0
    latency_ms = int(latency_ms) if latency_ms is not None else 0
    location   = _clamp(location or "", 512)
    marker     = _clamp(marker or "", 160)

    # ログ1行分テキスト（Locationがあれば Loc="..." を追加）
    parts = []
    if marker:
        parts.append(marker)
    parts.extend([
        f"{method} {path} {status}",
        f'UA="{ua}"',
        f'Ref="{referer}"',
        f'ep="{endpoint}"',
        f'user="{username}"',
    ])
    if location:
        parts.append(f'Loc="{location}"')
    parts.append(f"{latency_ms}ms")

    log_text = " ".join(parts)
    log_text = _clamp(log_text, 1024)

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            """
            INSERT INTO logs
              (log_date, ip, method, path, status, ua, referer, endpoint, username, latency_ms, log_text)
            VALUES
              (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (ip, method, path, status, ua, referer, endpoint, username, latency_ms, log_text),
        )
        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass


# ==========================================================
# 404バースト検知（メモリで確実にカウント）＋JP除外
#   - 60秒以内に同一 IP+Path で 3回 404 → Discord通知
#   - JP発IPは通知しない（ログは通常通り残す）
#   - クールダウン 3分（同一 IP+Path で再通知抑止）
#   - 軽量GCでメモリ肥大を抑止
# ==========================================================
_BURST_WINDOW_SEC = 60   # 60秒窓
_COOLDOWN_SEC = 180      # 3分クールダウン
_burst_hits: Dict[Tuple[str, str], List[float]] = {}             # {(ip, path): [timestamps]}
_burst_cooldown_until: Dict[Tuple[str, str], float] = {}         # {(ip, path): next_ts}

def _gc_dict(d: Dict, max_keys: int = 5000) -> None:
    """サイズ上限を超えたら先頭から雑に落とす簡易GC。"""
    try:
        if len(d) > max_keys:
            drop = len(d) - max_keys
            for i, k in enumerate(list(d.keys())):
                if i >= drop:
                    break
                d.pop(k, None)
    except Exception:
        pass

def _note_404_hit_and_should_notify(ip: str, path: str) -> tuple[bool, int, float]:
    """
    同一IP+Pathの404をメモリでカウントし、60秒内3回でTrue。クールダウンあり。
    return: (should_notify, current_count_in_window, cooldown_until_epoch)
    """
    now = time.time()
    key = (ip, path or "-")
    lst = [t for t in _burst_hits.get(key, []) if now - t <= _BURST_WINDOW_SEC]
    lst.append(now)
    _burst_hits[key] = lst
    cnt = len(lst)
    cooldown_until = _burst_cooldown_until.get(key, 0.0)
    should = cnt >= 3 and now >= cooldown_until
    _dbg(f"404-hit ip={ip} path={path} cnt={cnt} within={_BURST_WINDOW_SEC}s "
         f"cooldown_until={int(cooldown_until)} now={int(now)} should_notify={should}")
    if should:
        _burst_cooldown_until[key] = now + _COOLDOWN_SEC

    # たまにGC
    _gc_dict(_burst_hits)
    _gc_dict(_burst_cooldown_until)
    return should, cnt, _burst_cooldown_until.get(key, 0.0)

# ----------------------------------------------------------
# 追加: IP単位の404バースト検知（パス無視、WPスキャン等の対策）
# ----------------------------------------------------------
_IP_BURST_WINDOW_SEC = 60     # 60秒窓
_IP_BURST_THRESHOLD = 4       # 60秒以内に同一IPで4回以上404
_IP_COOLDOWN_SEC = 180        # 3分クールダウン

_ip_404_hits: Dict[str, List[float]] = {}       # {ip: [timestamps]}
_ip_cooldown_until: Dict[str, float] = {}       # {ip: next_ts}

def _note_404_hit_ip_only(ip: str) -> tuple[bool, int, float]:
    """
    同一IPの404をメモリでカウントし、60秒内が閾値以上でTrue。クールダウンあり。
    return: (should_notify, current_count_in_window, cooldown_until_epoch)
    """
    now = time.time()
    lst = [t for t in _ip_404_hits.get(ip, []) if now - t <= _IP_BURST_WINDOW_SEC]
    lst.append(now)
    _ip_404_hits[ip] = lst
    cnt = len(lst)
    cooldown_until = _ip_cooldown_until.get(ip, 0.0)
    should = cnt >= _IP_BURST_THRESHOLD and now >= cooldown_until
    _dbg(f"404-hit-ip ip={ip} cnt={cnt} within={_IP_BURST_WINDOW_SEC}s "
         f"cooldown_until={int(cooldown_until)} now={int(now)} should_notify={should}")
    if should:
        _ip_cooldown_until[ip] = now + _IP_COOLDOWN_SEC

    # たまにGC
    _gc_dict(_ip_404_hits)
    _gc_dict(_ip_cooldown_until)
    return should, cnt, _ip_cooldown_until.get(ip, 0.0)


def _country_code_from_request_ip(ip: str) -> Optional[str]:
    """CFヘッダまたはGeoLite2から国コード取得（設定があれば）。"""
    h = _req.headers.get("CF-IPCountry")
    if h:
        return h.strip()
    db_path = current_app.config.get("GEOIP2_COUNTRY_DB")
    if db_path:
        try:
            import geoip2.database
            with geoip2.database.Reader(db_path) as reader:
                return reader.country(ip).country.iso_code or None
        except Exception:
            return None
    return None


# ==========================================================
# Webhook 取得（users.admin 固定 → 見つからない時だけ保険）
# ==========================================================
def _get_discord_webhook_from_db() -> Optional[str]:
    """
    DBからWebhook URLを取得。
    仕様: users テーブルの username='admin' の webhook_url を使用。
    """
    db = None  # ← 例外時の finally 保護
    try:
        db = get_db()
        cur = db.cursor()

        # 1) users.username='admin' の webhook_url を最優先
        try:
            cur.execute(
                "SELECT webhook_url FROM users "
                "WHERE username = 'admin' "
                "AND webhook_url IS NOT NULL AND webhook_url <> '' "
                "ORDER BY updated_at DESC, id DESC LIMIT 1"
            )
            row = cur.fetchone()
        except Exception:
            # updated_at が無いスキーマの保険
            cur.execute(
                "SELECT webhook_url FROM users "
                "WHERE username = 'admin' "
                "AND webhook_url IS NOT NULL AND webhook_url <> '' "
                "ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()

        if row and row[0]:
            _dbg("webhook-source=db.users(admin).webhook_url")
            return row[0]

        # 2) 保険: settings(name='discord_webhook')
        try:
            cur.execute("SELECT value FROM settings WHERE name = 'discord_webhook' LIMIT 1")
            row = cur.fetchone()
            if row and row[0]:
                _dbg("webhook-source=db.settings")
                return row[0]
        except Exception:
            pass

    except Exception as e:
        _dbg(f"webhook-db-lookup failed err={e!r}")
    finally:
        try:
            if db is not None:
                db.close()
        except Exception:
            pass
    return None


def _read_first_existing_file(paths: List[str]) -> Optional[str]:
    """パス候補のうち、最初に見つかったテキストファイルの1行目を返す。"""
    for p in paths:
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    line = f.readline().strip()
                    if line:
                        _dbg(f"webhook-source=file:{p}")
                        return line
        except Exception as e:
            _dbg(f"webhook-file-read-failed path={p} err={e!r}")
    return None


def _get_discord_webhook() -> Optional[str]:
    """
    users('admin').webhook_url を最優先で取得。
    見つからない場合のみ、settings → 環境変数 → ファイルの順で保険。
    """
    # DB（正規仕様）
    url = _get_discord_webhook_from_db()
    if url:
        from app.discord_notifications.repository import get_discord_webhook
        return get_discord_webhook("suspicious_access", url) or None

    # 保険: config/env
    cfg = current_app.config if current_app else {}
    for key in ("DISCORD_WEBHOOK", "DISCORD_ALERT_WEBHOOK", "WEBHOOK_DISCORD", "DISCORD_URL", "DISCORD_WEBHOOK_URL"):
        val = (cfg.get(key) if cfg else None) or os.getenv(key)
        if val:
            _dbg(f"webhook-source=config:{key}")
            from app.discord_notifications.repository import get_discord_webhook
            return get_discord_webhook("suspicious_access", val) or None

    # 保険: ファイル
    file_url = _read_first_existing_file([
        "/mnt/mfu/discord_webhook.txt",
        "/mnt/mfu/app/discord_webhook.txt",
        "/etc/mfu/discord_webhook.txt",
    ])
    if file_url:
        from app.discord_notifications.repository import get_discord_webhook
        return get_discord_webhook("suspicious_access", file_url) or None

    _dbg("webhook-source=none")
    return None


def _send_discord(content: str) -> None:
    url = _get_discord_webhook()
    if not url:
        _dbg("discord-skip reason=no-webhook")
        return

    payload = {"content": content}
    body = json.dumps(payload).encode("utf-8")

    # requests があれば使う
    if requests is not None:
        try:
            _dbg(f"discord-send(using=requests) len={len(content)} url={url[:32]}...")
            resp = requests.post(url, json=payload, timeout=7)
            _dbg(f"discord-resp status={getattr(resp, 'status_code', '?')} ok={getattr(resp, 'ok', '?')}")
        except Exception as e:
            _dbg(f"discord-err(using=requests) err={e!r}")
        return

    # フォールバック: urllib
    try:
        _dbg(f"discord-send(using=urllib) len={len(content)} url={url[:32]}...")
        req = _urlreq.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with _urlreq.urlopen(req, timeout=7) as r:
            _dbg(f"discord-resp status={getattr(r, 'status', '?')}")
    except HTTPError as e:
        _dbg(f"discord-err(using=urllib) http={e.code} reason={e.reason}")
    except URLError as e:
        _dbg(f"discord-err(using=urllib) urlerr={e.reason!r}")
    except Exception as e:
        _dbg(f"discord-err(using=urllib) err={e!r}")


def _norm_cc_from_sources(*values) -> Optional[str]:
    """
    複数ソースの国名/国コードから JP 判定を統一。
    いずれかが 'JP' / 'JPN' / 'JAPAN' なら 'JP' を返す。
    それ以外は、最初に得られた2文字コードっぽい値を返すか None。
    """
    first_code = None
    for v in values:
        if not v:
            continue
        s = str(v).strip()
        if not s:
            continue
        su = s.upper()
        if su in ("JP", "JPN", "JAPAN"):
            return "JP"
        if first_code is None and len(su) == 2 and su.isalpha():
            first_code = su
    return first_code


def _fmt_provider(netinfo: dict) -> str:
    # whois_util.get_netinfo() の戻りに合わせて安全に取り出し
    org = (netinfo.get("org") or netinfo.get("orgname") or netinfo.get("org_name") or "").strip()
    asn = (netinfo.get("asname") or netinfo.get("asn") or netinfo.get("netname") or "").strip()
    if org and asn:
        return f"{org} / {asn}"
    return org or asn or "不明"


# ==========================================================
# after_request から呼び出すラッパ
# ==========================================================
def _client_ip() -> str:
    """Return only the address normalized by the trusted ProxyFix hop."""
    return _req.remote_addr or "-"


def _latency_ms() -> int:
    """before_request で mark_request_start() を仕込んである場合のみ計測。"""
    try:
        if hasattr(g, "request_time_start"):
            return int((time.time() - g.request_time_start) * 1000)
    except Exception:
        pass
    return 0


def build_access_log_fields(flask_request, flask_response, flask_session, endpoint: Optional[str]) -> dict:
    """DB保存用のフィールド辞書を作成（log_request_raw にそのまま渡せる形）。"""

    # --- user="" に入れる表示名を決定 ---
    username = ""
    try:
        getter = getattr(flask_session, "get", None)
        if getter:
            # ① 外部ログインユーザー（LINEログイン等）
            ext_user_id = flask_session.get("ext_user_id")
            nickname    = (flask_session.get("ext_user_nickname") or "").strip()

            if ext_user_id is not None:
                try:
                    ext_id_int = int(ext_user_id)
                except Exception:
                    ext_id_int = ext_user_id  # 念のためそのまま

                # 形式: LINE_通しID_ユーザー名
                # 例: user="LINE_123_いおりん"
                if nickname:
                    username = f"LINE_{ext_id_int}_{nickname}"
                else:
                    username = f"LINE_{ext_id_int}_"
            else:
                # ② 従来のMFU内部ログインユーザー（/login の username）
                raw = (flask_session.get("user") or "").strip()
                if raw:
                    username = raw
    except Exception:
        username = ""

    return dict(
        ip=_client_ip(),
        method=flask_request.method,
        path=flask_request.path or "",
        status=int(getattr(flask_response, "status_code", 0) or 0),
        ua=flask_request.headers.get("User-Agent", "-"),
        referer=flask_request.headers.get("Referer", ""),
        endpoint=endpoint or "",
        username=username,
        latency_ms=_latency_ms(),
        marker=getattr(g, "mfu_access_log_marker", ""),
    )


def _is_private_or_reserved_ip(ip: str) -> bool:
    try:
        ipobj = ipaddress.ip_address(ip)
        return (ipobj.is_private or ipobj.is_loopback or ipobj.is_link_local or
                ipobj.is_reserved or ipobj.is_multicast)
    except Exception:
        # 解析不能なら安全側（whoisしない）
        return True

# ----------------------------------------------------------
# アクセスログ抑制（3xxリダイレクトのバーストを汎用的に間引く）
# ----------------------------------------------------------

# key: (ip, path, status, ua) -> (last_ts, count)
_recent_access_hits: Dict[Tuple[str, str, int, str], Tuple[float, int]] = {}

_FW_404_RATE_THRESHOLD = max(1, int(os.getenv("FW_404_RATE_THRESHOLD", "8")))
_FW_404_RATE_WINDOW_SEC = max(0.1, float(os.getenv("FW_404_RATE_WINDOW_SEC", "10")))
_FW_404_IP_THRESHOLD = max(1, int(os.getenv("FW_404_IP_THRESHOLD", "20")))
_FW_404_IP_WINDOW_SEC = max(1.0, float(os.getenv("FW_404_IP_WINDOW_SEC", "300")))
_FW_BAN_COOLDOWN_SEC = max(1, int(os.getenv("FW_BAN_COOLDOWN_SEC", "60")))
_FW_404_IPV4_PREFIX = 32

_FW_404_SETTINGS_PATH = os.getenv("FW_404_SETTINGS_PATH", "/mnt/mfu/app/fw_404_settings.json")
_fw_404_settings_lock = threading.Lock()
_fw_auto_ban_schema_lock = threading.Lock()
_fw_auto_ban_schema_ready = False


def _normalize_fw_404_settings(src: dict | None) -> dict:
    src = src or {}
    mode = str(src.get("mode") or "observe").strip().lower()
    if mode not in {"observe", "enforce"}:
        mode = "observe"
    return {
        "mode": mode,
        "observe_until": str(src.get("observe_until") or "").strip(),
        "sensitive_window_sec": max(1.0, float(src.get("sensitive_window_sec", 60))),
        "sensitive_threshold": max(1, int(src.get("sensitive_threshold", 2))),
        "short_window_sec": max(0.1, float(src.get("short_window_sec", _FW_404_RATE_WINDOW_SEC))),
        "short_threshold": max(1, int(src.get("short_threshold", _FW_404_RATE_THRESHOLD))),
        "ip_window_sec": max(1.0, float(src.get("ip_window_sec", _FW_404_IP_WINDOW_SEC))),
        "ip_threshold": max(1, int(src.get("ip_threshold", _FW_404_IP_THRESHOLD))),
        "cooldown_sec": max(1, int(src.get("cooldown_sec", _FW_BAN_COOLDOWN_SEC))),
        "ban_duration_sec": max(60, min(604800, int(src.get("ban_duration_sec", 3600)))),
        "repeat_ban_duration_sec": max(60, min(604800, int(src.get("repeat_ban_duration_sec", 86400)))),
        "generic_third_ban_duration_sec": max(60, min(604800, int(src.get("generic_third_ban_duration_sec", 604800)))),
        "sensitive_permanent_threshold": max(3, min(100, int(src.get("sensitive_permanent_threshold", 3)))),
        "generic_permanent_threshold": max(4, min(100, int(src.get("generic_permanent_threshold", 4)))),
        "repeat_window_sec": max(3600, min(31536000, int(src.get("repeat_window_sec", 2592000)))),
        "ipv4_prefix": 24 if str(src.get("ipv4_prefix", _FW_404_IPV4_PREFIX)) != "32" else 32,
    }


def _load_fw_404_settings_from_file() -> dict:
    try:
        with open(_FW_404_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return _normalize_fw_404_settings(data)
    except Exception:
        pass
    return _normalize_fw_404_settings({})


_fw_404_settings = _load_fw_404_settings_from_file()
try:
    _fw_404_settings_mtime = os.path.getmtime(_FW_404_SETTINGS_PATH)
except OSError:
    _fw_404_settings_mtime = 0.0


def get_fw_404_settings() -> dict:
    global _fw_404_settings_mtime
    try:
        current_mtime = os.path.getmtime(_FW_404_SETTINGS_PATH)
    except OSError:
        current_mtime = 0.0
    with _fw_404_settings_lock:
        if current_mtime != _fw_404_settings_mtime:
            _fw_404_settings.clear()
            _fw_404_settings.update(_load_fw_404_settings_from_file())
            _fw_404_settings_mtime = current_mtime
        return dict(_fw_404_settings)


def save_fw_404_settings(settings: dict) -> dict:
    global _fw_404_settings_mtime
    normalized = _normalize_fw_404_settings(settings)
    os.makedirs(os.path.dirname(_FW_404_SETTINGS_PATH), exist_ok=True)
    with _fw_404_settings_lock:
        temp_path = f"{_FW_404_SETTINGS_PATH}.{os.getpid()}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, _FW_404_SETTINGS_PATH)
        _fw_404_settings.clear()
        _fw_404_settings.update(normalized)
        _fw_404_settings_mtime = os.path.getmtime(_FW_404_SETTINGS_PATH)
    return dict(normalized)


def _is_public_ip(ip: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip)
        return not (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        )
    except Exception:
        return False


def _write_fw_ban_log(ip: str, log_text: str) -> None:
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO logs (log_date, ip, log_text) VALUES (NOW(), %s, %s)",
            (ip or "-", _clamp(log_text, 1024)),
        )
        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass


def _ensure_fw_auto_ban_schema(db) -> None:
    global _fw_auto_ban_schema_ready
    if _fw_auto_ban_schema_ready:
        return
    with _fw_auto_ban_schema_lock:
        if _fw_auto_ban_schema_ready:
            return
        cur = db.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS fw_auto_ban_decisions (
                id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                decision_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                ip VARCHAR(64) NOT NULL,
                target VARCHAR(96) NOT NULL,
                reason VARCHAR(32) NOT NULL,
                evidence_count INT NOT NULL DEFAULT 0,
                distinct_paths INT NOT NULL DEFAULT 0,
                sample_paths TEXT NULL,
                mode VARCHAR(16) NOT NULL,
                status VARCHAR(24) NOT NULL,
                ban_duration_sec INT NULL,
                expires_at DATETIME NULL,
                offense_number INT NOT NULL DEFAULT 1,
                escalation_class VARCHAR(16) NOT NULL DEFAULT 'generic',
                action_kind VARCHAR(16) NOT NULL DEFAULT 'temporary',
                error_text VARCHAR(1024) NULL,
                PRIMARY KEY (id),
                KEY idx_fw_auto_target_time (target, decision_at),
                KEY idx_fw_auto_ip_time (ip, decision_at),
                KEY idx_fw_auto_status_time (status, decision_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        for column_name, definition in (
            ("offense_number", "INT NOT NULL DEFAULT 1"),
            ("escalation_class", "VARCHAR(16) NOT NULL DEFAULT 'generic'"),
            ("action_kind", "VARCHAR(16) NOT NULL DEFAULT 'temporary'"),
        ):
            cur.execute("SHOW COLUMNS FROM fw_auto_ban_decisions LIKE %s", (column_name,))
            if not cur.fetchone():
                cur.execute(
                    f"ALTER TABLE fw_auto_ban_decisions ADD COLUMN {column_name} {definition}"
                )
        db.commit()
        _fw_auto_ban_schema_ready = True


def _auto_ban_target(ip: str, settings: dict) -> dict:
    ip_obj = ipaddress.ip_address(ip)
    if ip_obj.version == 4:
        network = ipaddress.ip_network(
            f"{ip}/{int(settings['ipv4_prefix'])}",
            strict=False,
        )
        return {"version": 4, "target": str(network)}
    return {"version": 6, "target": f"{ip_obj}/128"}


def _maybe_auto_ban_by_404_rate(
    ip: str,
    path: str,
    *,
    endpoint: str = "",
) -> None:
    """Evaluate 404s centrally so every Gunicorn worker sees the same evidence."""
    if not _is_public_ip(ip):
        return

    settings = get_fw_404_settings()
    target = _auto_ban_target(ip, settings)
    target_repr = target["target"]
    lock_name = f"mfu-fwban:{ip}"[:64]
    max_window = int(max(
        float(settings["sensitive_window_sec"]),
        float(settings["short_window_sec"]),
        float(settings["ip_window_sec"]),
    ))
    decision_id = None
    evidence = None
    enforce = False
    duration = None
    action_kind = "temporary"
    escalation_class = "generic"
    offense_number = 1
    db = get_db()
    lock_acquired = False
    try:
        _ensure_fw_auto_ban_schema(db)
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT GET_LOCK(%s, 2) AS acquired", (lock_name,))
        lock_acquired = bool((cur.fetchone() or {}).get("acquired"))
        if not lock_acquired:
            current_app.logger.warning("FW auto-BAN lock timeout ip=%s", ip)
            return

        cur.execute("SELECT NOW() AS db_now")
        db_now = (cur.fetchone() or {}).get("db_now") or datetime.now()
        cur.execute(
            f"""
            SELECT log_date, path, status, endpoint
              FROM logs
             WHERE ip=%s
               AND log_date >= DATE_SUB(NOW(), INTERVAL {max_window} SECOND)
               AND status IN (400, 403, 404, 405)
             ORDER BY log_date, id
            """,
            (ip,),
        )
        rows = cur.fetchall()
        # The current 404 has already been committed to logs. The explicit
        # fallback only matters in tests or if the structured insert changes.
        if not any(str(row.get("path") or "") == path for row in rows):
            rows.append({
                "log_date": db_now,
                "path": path,
                "status": 404,
                "endpoint": endpoint,
            })
        evidence = evaluate_events(rows, settings, now=db_now)
        if evidence is None:
            return
        escalation_class = "sensitive" if evidence.reason == "sensitive" else "generic"

        cur.execute(
            """
            SELECT id
              FROM fw_auto_ban_decisions
             WHERE target=%s
               AND status IN ('observed','pending','added','already','permanent')
               AND decision_at >= DATE_SUB(NOW(), INTERVAL %s SECOND)
             ORDER BY id DESC
             LIMIT 1
            """,
            (target_repr, int(settings["cooldown_sec"])),
        )
        if cur.fetchone():
            return

        enforce = enforcement_enabled(settings)
        if enforce:
            reason_clause = "reason='sensitive'" if escalation_class == "sensitive" else "reason IN ('short','cumulative')"
            cur.execute(
                f"""
                SELECT COUNT(*) AS prior_count
                  FROM fw_auto_ban_decisions
                 WHERE target=%s
                   AND status IN ('added','already','permanent')
                   AND {reason_clause}
                   AND decision_at >= DATE_SUB(NOW(), INTERVAL %s SECOND)
                """,
                (target_repr, int(settings["repeat_window_sec"])),
            )
            prior_count = int((cur.fetchone() or {}).get("prior_count") or 0)
            escalation = choose_ban_escalation(
                prior_count=prior_count,
                escalation_class=escalation_class,
                settings=settings,
            )
            offense_number = escalation.offense_number
            action_kind = escalation.action_kind
            duration = escalation.duration_sec

        cur.execute(
            """
            INSERT INTO fw_auto_ban_decisions
                (ip, target, reason, evidence_count, distinct_paths,
                 sample_paths, mode, status, ban_duration_sec,
                 offense_number, escalation_class, action_kind)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                ip,
                target_repr,
                evidence.reason,
                evidence.count,
                evidence.distinct_paths,
                json.dumps(evidence.sample_paths, ensure_ascii=False),
                "enforce" if enforce else "observe",
                "pending" if enforce else "observed",
                duration,
                offense_number,
                escalation_class,
                action_kind,
            ),
        )
        decision_id = int(cur.lastrowid)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        current_app.logger.warning("FW auto-BAN evaluation failed ip=%s path=%s", ip, path, exc_info=True)
        return
    finally:
        if lock_acquired:
            try:
                release_cur = db.cursor()
                release_cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
            except Exception:
                pass
        try:
            db.close()
        except Exception:
            pass

    samples = ",".join(evidence.sample_paths if evidence else ())
    if not enforce:
        _write_fw_ban_log(
            ip,
            f"[FW_BAN][OBSERVE] ip={ip} target={target_repr} reason={evidence.reason} "
            f"count={evidence.count} distinct={evidence.distinct_paths} samples={samples}",
        )
        return

    if action_kind == "permanent":
        result = permanently_ban_ip_cidr_via_ssh(target)
    else:
        result = temporarily_ban_ip_cidr_via_ssh(target, timeout_sec=int(duration or 3600))
    result_status = str(result.get("status") or "error")
    update_db = get_db()
    try:
        update_cur = update_db.cursor()
        if result.get("ok"):
            if action_kind == "permanent":
                update_cur.execute(
                    """
                    UPDATE fw_auto_ban_decisions
                       SET status='permanent', expires_at=NULL
                     WHERE id=%s
                    """,
                    (decision_id,),
                )
            else:
                update_cur.execute(
                    """
                    UPDATE fw_auto_ban_decisions
                       SET status=%s,
                           expires_at=DATE_ADD(NOW(), INTERVAL %s SECOND)
                     WHERE id=%s
                    """,
                    (result_status, int(duration or 3600), decision_id),
                )
        else:
            update_cur.execute(
                """
                UPDATE fw_auto_ban_decisions
                   SET status='failed', error_text=%s
                 WHERE id=%s
                """,
                (_clamp(result.get("stderr") or result_status, 1024), decision_id),
            )
        update_db.commit()
    finally:
        update_db.close()

    if result.get("ok"):
        marker = "PERMANENT" if action_kind == "permanent" else "AUTO"
        _write_fw_ban_log(
            ip,
            f"[FW_BAN][{marker}] ip={ip} target={target_repr} reason={evidence.reason} "
            f"offense={offense_number} class={escalation_class} count={evidence.count} "
            f"distinct={evidence.distinct_paths} duration={duration or 'permanent'} "
            f"status={result_status} samples={samples}",
        )
    else:
        current_app.logger.warning(
            "[FW_BAN][AUTO] failed ip=%s target=%s reason=%s status=%s stderr=%s",
            ip,
            target_repr,
            evidence.reason,
            result_status,
            result.get("stderr", ""),
        )


def list_fw_auto_permanent_bans(limit: int = 200) -> list[dict]:
    db = get_db()
    try:
        _ensure_fw_auto_ban_schema(db)
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, decision_at, ip, target, reason, evidence_count,
                   distinct_paths, sample_paths, offense_number,
                   escalation_class, action_kind
              FROM fw_auto_ban_decisions
             WHERE status='permanent'
             ORDER BY decision_at DESC, id DESC
             LIMIT %s
            """,
            (max(1, min(1000, int(limit))),),
        )
        return cur.fetchall() or []
    finally:
        db.close()


def unban_fw_auto_permanent(decision_id: int, *, actor: str = "admin") -> dict:
    db = get_db()
    try:
        _ensure_fw_auto_ban_schema(db)
        cur = db.cursor(dictionary=True)
        cur.execute(
            """
            SELECT id, ip, target
              FROM fw_auto_ban_decisions
             WHERE id=%s AND status='permanent'
             LIMIT 1
            """,
            (int(decision_id),),
        )
        row = cur.fetchone()
    finally:
        db.close()
    if not row:
        return {"ok": False, "status": "not_found", "message": "有効な自動永久BANが見つかりません。"}

    network = ipaddress.ip_network(str(row["target"]), strict=False)
    target = {"version": network.version, "target": str(network)}
    result = unban_auto_permanent_ip_cidr_via_ssh(target)
    if not result.get("ok"):
        return result

    update_db = get_db()
    try:
        update_cur = update_db.cursor()
        update_cur.execute(
            """
            UPDATE fw_auto_ban_decisions
               SET status='unbanned', error_text=%s
             WHERE target=%s AND status='permanent'
            """,
            (f"manual unban by {actor}"[:1024], row["target"]),
        )
        update_db.commit()
    finally:
        update_db.close()
    _write_fw_ban_log(
        str(row.get("ip") or "-"),
        f"[FW_BAN][UNBAN] target={row['target']} actor={actor} status={result.get('status')}",
    )
    return result


def _should_suppress_access_log(fields: dict) -> bool:
    """
    3xxリダイレクトが同一IP/Path/UA/statusで瞬間的に連打された場合、
    DBへのINSERTを間引く。

    - 対象 status は 301/302/303/307/308
    - 直前の記録から1秒以内の同一キーは「抑制」扱い
      （最初の1件だけ残して、同じキーの連打分は捨てる）
    """
    try:
        status = int(fields.get("status") or 0)

        # リダイレクト以外は触らない
        if status not in (301, 302, 303, 307, 308):
            return False

        ip = fields.get("ip") or "-"
        path = fields.get("path") or "-"
        ua = fields.get("ua") or "-"
        key = (ip, path, status, ua)

        now = time.time()
        last_ts, cnt = _recent_access_hits.get(key, (0.0, 0))

        # 直前記録から1秒以内ならログ抑制
        if now - last_ts <= 1.0:
            new_cnt = cnt + 1
            _recent_access_hits[key] = (now, new_cnt)
            _dbg(
                f"suppress-log(reason=burst-redirect-generic) ip={ip} "
                f"path={path} status={status} count={new_cnt}"
            )

            # 簡易GC（古いエントリを掃除）
            if len(_recent_access_hits) > 5000:
                for i, k in enumerate(list(_recent_access_hits.keys())):
                    if i >= 1000:
                        break
                    ts, _ = _recent_access_hits.get(k, (0.0, 0))
                    if now - ts > 60.0:
                        _recent_access_hits.pop(k, None)

            return True

        # 1秒以上空いていれば新規記録として扱う（＝この1件はログに残す）
        _recent_access_hits[key] = (now, 1)
        return False

    except Exception as e:
        _dbg(f"suppress-check-failed err={e!r}")
        return False


def log_access(flask_request, flask_response, flask_session, *, endpoint: Optional[str]) -> None:
    """
    after_requestから呼び出すだけでOK
    - 除外判定→log_request_rawでINSERT
    - 404ならDB共有の自動BAN判定と、非JP向けDiscord通知を実行
    """
    path = flask_request.path or ""
    if getattr(g, "mfu_skip_access_log", False):
        return
    if should_skip_access_log(path, endpoint):
        return

    fields = build_access_log_fields(flask_request, flask_response, flask_session, endpoint)

    # 302リダイレクト系の連打アクセスは、特定endpointのみログを間引く
    # （_should_suppress_access_log は logs.py 内のヘルパー）
    if _should_suppress_access_log(fields):
        return

    # Location ヘッダ（リダイレクト先など）を取得
    try:
        location = flask_response.headers.get("Location")
    except Exception:
        location = None

    try:
        # まずDBへ記録
        log_request_raw(location=location, **fields)
    except Exception as e:
        _dbg(f"log_access: INSERT failed err={e!r} fields={fields}")
        current_app.logger.warning(f"log_access: log_request_raw failed: {e}")
        return  # 記録失敗時は以降スキップ

    # 404のセキュリティ判定
    try:
        if fields.get("status") == 404 and fields.get("ip"):
            ip = fields["ip"]

            # 全Gunicornワーカーで共有するリスク別自動BAN判定
            _maybe_auto_ban_by_404_rate(
                ip,
                path,
                endpoint=fields.get("endpoint") or "",
            )

            # 既存: IP+Path 単位
            notify_path, cnt_path, cooldown_until_path = _note_404_hit_and_should_notify(ip, path)
            # 追加: IP 単位
            notify_ip, cnt_ip, cooldown_until_ip = _note_404_hit_ip_only(ip)

            # 国コードは CF/GeoIP2 と whois 両方を参照し、どちらかがJPなら通知抑止
            cc_hdr = (_country_code_from_request_ip(ip) or "").strip()
            netinfo = {}
            cc_whois = ""
            if not _is_private_or_reserved_ip(ip):
                netinfo = get_netinfo(ip) or {}
                cc_whois = (netinfo.get("country_code")
                            or netinfo.get("cc")
                            or netinfo.get("country")
                            or netinfo.get("country_name")
                            or "")
            cc = _norm_cc_from_sources(cc_hdr, cc_whois)

            _dbg(
                f"404-eval ip={ip} path={path} "
                f"by_path(cnt={cnt_path}, notify={notify_path}) "
                f"by_ip(cnt={cnt_ip}, notify={notify_ip}) "
                f"cc_hdr={cc_hdr!r} cc_whois={cc_whois!r} cc_norm={cc!r}"
            )

            if cc == "JP":
                _dbg(f"notify-suppressed reason=JP ip={ip} path={path}")
                return  # JPは通知しない

            # どちらかがTrueなら通知
            if notify_path or notify_ip:
                provider = _fmt_provider(netinfo)
                country_disp = (netinfo.get("country") or cc or "??")
                ua = flask_request.headers.get("User-Agent", "-")

                trigger = "by_path" if notify_path else "by_ip"
                msg = (
                    f":rotating_light: 404 burst detected (non-JP) [{trigger}]\n"
                    f"・IPアドレス: {ip}\n"
                    f"・Path: {path}\n"
                    f"・国: {country_disp}\n"
                    f"・プロバイダ: {provider}\n"
                    f"・UA: {ua}"
                )
                _dbg(
                    f"discord-post trigger={trigger} ip={ip} "
                    f"path={path} country={country_disp} provider={provider}"
                )
                _send_discord(msg)
            else:
                _dbg(
                    f"notify-deferred ip={ip} path={path} "
                    f"cooldowns=({int(cooldown_until_path)},{int(cooldown_until_ip)})"
                )
    except Exception as e:
        _dbg(f"log_access: burst notify failed err={e!r}")
        current_app.logger.warning("log_access: burst notify failed", exc_info=True)


# ==========================================================
# ログイン系（互換）
# ==========================================================
def write_login_log(username: str, ip: str, tag: str = "LOGIN") -> None:
    """ログインイベントの簡易ログ（従来互換）。"""
    db = get_db()
    try:
        tag = (tag or "LOGIN").strip()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO logs (log_date, ip, log_text) VALUES (NOW(), %s, %s)",
            (ip or "-", f"[{tag}] ユーザー: {username} がログインしました"),
        )
        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass


def write_line_login_log(nickname: str, ip: str, user_id: int | None = None) -> None:
    """LINEログイン簡易ログ（従来互換）。"""
    nick = (nickname or "未設定").strip()
    if user_id is not None:
        log_text = f"[LINE_LOGIN] ユーザー: #{int(user_id)} {nick} がログインしました"
    else:
        log_text = f"[LINE_LOGIN] ユーザー: {nick} がログインしました"

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO logs (log_date, ip, log_text) VALUES (NOW(), %s, %s)",
            (ip or "-", log_text),
        )
        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass


def write_line_login_blocked_log(
    ip: str,
    *,
    original_user_id: int | None = None,
) -> None:
    """退会済みLINEアカウントのログイン拒否を管理ログへ記録する。"""
    log_text = "[LINE_LOGIN_BLOCKED] 退会済みLINEアカウントからのログインを拒否しました"
    if original_user_id is not None:
        log_text += f" original_user_id={int(original_user_id)}"

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO logs (log_date, ip, log_text) VALUES (NOW(), %s, %s)",
            (ip or "-", log_text),
        )
        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass


def write_smtp_log(log_text: str, ip: str = "-") -> None:
    """SMTP送信結果を logs テーブルに1行だけ書き込む簡易ログ。"""
    log_text = _clamp(log_text or "", 1024)

    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(
            "INSERT INTO logs (log_date, ip, log_text) VALUES (NOW(), %s, %s)",
            (ip or "-", log_text),
        )
        db.commit()
    finally:
        try:
            db.close()
        except Exception:
            pass


# ==========================================================
# Flask からの呼び出し用ヘルパ（任意）
# ==========================================================
def mark_request_start() -> None:
    """before_request で呼んでおくとレイテンシ測定に使える。"""
    try:
        g.request_time_start = time.time()
    except Exception:
        pass


def log_from_flask_response(resp, username: str = "") -> None:
    """
    既存コード互換の簡易ラッパ（未使用なら無視でOK）。
    """
    req = _req
    try:
        ip = (req.headers.get("X-Forwarded-For", "").split(",")[0].strip() or req.remote_addr or "-")
        method = req.method
        path = req.path
        status = getattr(resp, "status_code", None) or 0
        ua = req.headers.get("User-Agent", "")
        referer = req.headers.get("Referer", "")
        endpoint = getattr(req, "endpoint", None)
        latency_ms = 0
        try:
            started = getattr(g, "request_time_start", None)
            if started:
                latency_ms = int((time.time() - started) * 1000)
        except Exception:
            pass

        try:
            location = resp.headers.get("Location")
        except Exception:
            location = None

        log_request_raw(
            ip=ip,
            method=method,
            path=path,
            status=status,
            ua=ua,
            referer=referer,
            endpoint=endpoint or "",
            username=username or "",
            latency_ms=latency_ms,
            location=location,
        )
    except Exception:
        current_app.logger.warning("log_from_flask_response failed", exc_info=True)
