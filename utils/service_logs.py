# /mnt/mfu/app/utils/service_logs.py
# リアルタイム journalctl / tail ビューア（SSE）
# - admin のみアクセス可（Flask-Login か session/g の is_admin/role を判定）
# - JSON設定: 単一/複数 unit, もしくは /var/log/* のファイル追尾に対応

from flask import Blueprint, Response, render_template, request, abort, stream_with_context, session, g
import subprocess
import time
import contextlib
import json
import os
import re
from typing import Dict, Tuple, List, Union, Optional, Any

bp_service_logs = Blueprint("service_logs", __name__, url_prefix="/service_logs")

# ============ アクセス制御（admin限定） ============
# 既存の import に追加
from flask import Blueprint, Response, render_template, request, abort, stream_with_context, session, g, redirect, url_for

# === ここから置き換え ===
def _is_admin_user() -> bool:
    # 1) あなたの実装に合わせた最優先チェック
    try:
        if session.get("user") == "admin":
            return True
    except Exception:
        pass

    # 2) Flask-Login を使っている場合（念のため互換）
    try:
        from flask_login import current_user
        if getattr(current_user, "is_authenticated", False):
            if getattr(current_user, "is_admin", False):
                return True
            # username/id が admin の場合も許可
            if getattr(current_user, "username", None) == "admin":
                return True
            if str(getattr(current_user, "id", "")).lower() == "admin":
                return True
            role = getattr(current_user, "role", None)
            if isinstance(role, str) and role.lower() == "admin":
                return True
    except Exception:
        pass

    # 3) セッション/グローバルのフォールバック
    try:
        if session.get("is_admin") or (isinstance(session.get("role"), str) and session.get("role").lower() == "admin"):
            return True
    except Exception:
        pass
    try:
        if getattr(g, "is_admin", False):
            return True
        role = getattr(g, "role", None)
        if isinstance(role, str) and role.lower() == "admin":
            return True
    except Exception:
        pass
    return False

@bp_service_logs.before_request
def _require_admin():
    if not _is_admin_user():
        # 未ログインならログイン画面へ、ログイン済みで権限なしは403
        if not session.get("user"):
            return redirect(url_for("login"))
        abort(403, description="管理者のみアクセス可能")

# ============ 設定ファイル探索 ============
CONFIG_CANDIDATES = [
    os.getenv("MFU_SERVICE_LOGS_JSON"),
    "/mnt/mfu/data/service_logs.json",
    "/mnt/mfu/config/service_logs.json",
]

def _preferred_config_path() -> str:
    for p in CONFIG_CANDIDATES:
        if p:
            return p
    return "/mnt/mfu/data/service_logs.json"

# 既定値
_DEFAULT_UNITS: Dict[str, Union[str, List[str], Dict[str, str]]] = {
    "mfu":   "mfu.service",
    "thumb": "thumb_worker.service",
}
_DEFAULT_LABELS: Dict[str, str] = {
    k: (v if isinstance(v, str) else ", ".join(v) if isinstance(v, list) else v.get("file", "item"))
    for k, v in _DEFAULT_UNITS.items()
}
_DEFAULT_INITIAL = "mfu"
_DEFAULT_LINES = 200

_cfg_cache = {
    "path": None,
    "mtime": None,
    "units": _DEFAULT_UNITS,    # key -> "a.service" | ["a.service","b.service"] | {"file":"/var/log/.."}
    "labels": _DEFAULT_LABELS,
    "initial": _DEFAULT_INITIAL,
    "lines": _DEFAULT_LINES,
}

# ============ バリデーション ============
_key_pat = re.compile(r"^[a-z0-9_-]{1,32}$")
_unit_pat = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")  # 必要なら .timer 等を追加

def _is_allowed_file(path: str) -> bool:
    try:
        real = os.path.realpath(path)
    except Exception:
        return False
    return real.startswith("/var/log/")  # /var/log 配下のみ許可

def _normalize_units_entry(key: str, val) -> Optional[Tuple[Any, str]]:
    if not isinstance(key, str) or not _key_pat.match(key):
        return None

    # 文字列（単一ユニット）
    if isinstance(val, str):
        if not _unit_pat.match(val):
            return None
        return val, val

    # dict
    if isinstance(val, dict):
        # {"unit": "...", "label": "..."}
        if "unit" in val and isinstance(val.get("unit"), str):
            unit = val["unit"]
            if not _unit_pat.match(unit):
                return None
            label = val.get("label") or unit
            return unit, (label if isinstance(label, str) else unit)

        # {"units": ["...","..."], "label": "..."}
        if "units" in val and isinstance(val.get("units"), list):
            lst: List[str] = []
            for u in val["units"]:
                if isinstance(u, str) and _unit_pat.match(u):
                    lst.append(u)
            if not lst:
                return None
            label = val.get("label") or ", ".join(lst)
            return lst, (label if isinstance(label, str) else ", ".join(lst))

        # {"file": "/var/log/xxx.log", "label": "..."}
        if "file" in val and isinstance(val.get("file"), str):
            fpath = val["file"]
            if not _is_allowed_file(fpath):
                return None
            label = val.get("label") or fpath
            return {"file": fpath}, (label if isinstance(label, str) else fpath)

    return None

def _load_config_from_file(path: str):
    units: Dict[str, Union[str, List[str], Dict[str, str]]] = {}
    labels: Dict[str, str] = {}

    if not os.path.exists(path):
        return _DEFAULT_UNITS, _DEFAULT_LABELS, _DEFAULT_INITIAL, _DEFAULT_LINES

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    raw_units = data.get("units") if isinstance(data, dict) and "units" in data else data
    if not isinstance(raw_units, dict):
        raise ValueError("units must be an object")

    for key, val in raw_units.items():
        norm = _normalize_units_entry(key, val)
        if norm is None:
            continue
        spec, label = norm
        units[key] = spec
        labels[key] = label

    if not units:
        units = _DEFAULT_UNITS.copy()
        labels = {
            k: (v if isinstance(v, str) else ", ".join(v) if isinstance(v, list) else v.get("file", "item"))
            for k, v in units.items()
        }

    defaults = data.get("defaults") if isinstance(data, dict) else {}
    initial = defaults.get("initial") if isinstance(defaults, dict) else None
    if initial not in units:
        initial = next(iter(units.keys()), _DEFAULT_INITIAL)

    lines = defaults.get("lines") if isinstance(defaults, dict) else None
    try:
        lines = int(lines)
    except Exception:
        lines = _DEFAULT_LINES
    lines = max(50, min(2000, lines))

    return units, labels, initial, lines

def _get_config():
    path = _preferred_config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None

    if path != _cfg_cache["path"] or mtime != _cfg_cache["mtime"]:
        try:
            units, labels, initial, lines = _load_config_from_file(path)
            _cfg_cache.update({
                "path": path, "mtime": mtime,
                "units": units, "labels": labels,
                "initial": initial, "lines": lines,
            })
        except Exception:
            _cfg_cache.update({
                "path": path, "mtime": mtime,
                "units": _DEFAULT_UNITS, "labels": _DEFAULT_LABELS,
                "initial": _DEFAULT_INITIAL, "lines": _DEFAULT_LINES,
            })
    return _cfg_cache["units"], _cfg_cache["labels"], _cfg_cache["initial"], _cfg_cache["lines"]

# ============ ストリーム実装 ============
def _popen_lines(args: List[str]):
    p = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1
    )
    try:
        yield ": start\n\n"
        last_ping = time.time()
        for line in iter(p.stdout.readline, ""):
            data = line.rstrip("\r\n")
            yield f"data: {data}\n\n"
            now = time.time()
            if now - last_ping > 15:
                yield "event: ping\ndata: keepalive\n\n"
                last_ping = now
    finally:
        with contextlib.suppress(Exception):
            p.terminate()
            try:
                p.wait(timeout=1)
            except subprocess.TimeoutExpired:
                p.kill()

def _stream(spec: Union[str, List[str], Dict[str, str]], lines: int = 200):
    # ファイル追尾
    if isinstance(spec, dict) and "file" in spec:
        fpath = spec["file"]
        args = ["tail", "-n", str(lines), "-F", fpath]
        yield from _popen_lines(args)
        return
    # journalctl
    args = ["journalctl", "-f", "-n", str(lines), "--no-pager", "-o", "short-iso-precise"]
    if isinstance(spec, (list, tuple)):
        for u in spec:
            args += ["-u", u]
    else:
        args += ["-u", spec]
    yield from _popen_lines(args)

# ============ ルート ============
@bp_service_logs.get("/sse")
def sse():
    units, labels, initial, default_lines = _get_config()
    key = (request.args.get("service") or initial).lower()
    spec = units.get(key)
    if spec is None:
        abort(400, "unknown service")

    try:
        n = int(request.args.get("n", str(default_lines)))
    except ValueError:
        n = default_lines
    n = max(50, min(2000, n))

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return Response(
        stream_with_context(_stream(spec, n)),
        mimetype="text/event-stream",
        headers=headers,
    )

@bp_service_logs.get("/")
def page():
    units, labels, initial, default_lines = _get_config()
    return render_template(
        "service_logs.html",
        units=units,
        labels=labels,
        initial=initial,
        default_lines=default_lines
    )
