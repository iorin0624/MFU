# /mnt/mfu/app/utils/service_logs.py
# リアルタイム journalctl / tail ビューア（SSE）
# - admin のみアクセス可（Flask-Login か session/g の is_admin/role を判定）
# - JSON設定: 単一/複数 unit, もしくは /var/log/* のファイル追尾に対応

from flask import Blueprint, Response, render_template, request, abort, stream_with_context, session, g
import subprocess
import time
import contextlib
import csv
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Tuple, List, Union, Optional, Any

bp_service_logs = Blueprint("service_logs", __name__, url_prefix="/service_logs")

# ============ アクセス制御（admin限定） ============
# 既存の import に追加
from flask import Blueprint, Response, render_template, request, abort, stream_with_context, session, g, redirect, url_for, jsonify

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
JST = timezone(timedelta(hours=9))
HISTORY_MAX_DAYS = 31
HISTORY_TIMEOUT_SECONDS = 30
HISTORY_PAGE_SIZES = (25, 50, 100, 200)
HISTORY_CSV_MAX_ROWS = 100_000
_PRIORITY_NAMES = {
    0: "emergency",
    1: "alert",
    2: "critical",
    3: "error",
    4: "warning",
    5: "notice",
    6: "info",
    7: "debug",
}

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


# ============ 履歴検索 ============
class HistoryQueryError(ValueError):
    pass


def _history_units(spec: Union[str, List[str], Dict[str, str]]) -> List[str]:
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, (list, tuple)):
        return [str(unit) for unit in spec]
    raise HistoryQueryError("この項目はファイル追尾専用のため、履歴検索には対応していません。")


def _parse_history_datetime(raw: str, *, default: datetime) -> datetime:
    value = (raw or "").strip()
    if not value:
        return default
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HistoryQueryError("日時の形式が正しくありません。") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=JST)
    return parsed.astimezone(JST)


def _history_query_from_args(args, *, now: Optional[datetime] = None) -> dict:
    units, labels, initial, _ = _get_config()
    key = (args.get("service") or initial).strip().lower()
    spec = units.get(key)
    if spec is None:
        raise HistoryQueryError("不明なサービスです。")

    current = (now or datetime.now(JST)).astimezone(JST)
    default_from = current.replace(hour=0, minute=0, second=0, microsecond=0)
    date_from = _parse_history_datetime(args.get("date_from", ""), default=default_from)
    date_to = _parse_history_datetime(args.get("date_to", ""), default=current)
    if date_to < date_from:
        raise HistoryQueryError("終了日時は開始日時以降にしてください。")
    if date_to - date_from > timedelta(days=HISTORY_MAX_DAYS):
        raise HistoryQueryError(f"検索期間は最大{HISTORY_MAX_DAYS}日です。")

    level = (args.get("level") or "all").strip().lower()
    if level not in {"all", "warning", "error"}:
        raise HistoryQueryError("重要度の指定が正しくありません。")

    keyword = (args.get("keyword") or "").strip()
    if len(keyword) > 200:
        raise HistoryQueryError("キーワードは200文字以内にしてください。")

    try:
        page = max(1, int(args.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(args.get("page_size") or 50)
    except (TypeError, ValueError):
        page_size = 50
    if page_size not in HISTORY_PAGE_SIZES:
        page_size = 50

    return {
        "service": key,
        "label": labels.get(key, key),
        "units": _history_units(spec),
        "date_from": date_from,
        "date_to": date_to,
        "level": level,
        "keyword": keyword,
        "page": page,
        "page_size": page_size,
    }


def _journal_message(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
        return bytes(value).decode("utf-8", errors="replace")
    if value is None:
        return ""
    return str(value)


def _decode_journal_entry(entry: dict) -> dict:
    try:
        priority = int(entry.get("PRIORITY", 6))
    except (TypeError, ValueError):
        priority = 6
    priority = min(7, max(0, priority))

    try:
        timestamp_us = int(entry.get("__REALTIME_TIMESTAMP", 0))
        occurred_at = datetime.fromtimestamp(timestamp_us / 1_000_000, tz=JST)
        timestamp = occurred_at.isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OSError, OverflowError):
        timestamp = ""

    unit = str(entry.get("_SYSTEMD_UNIT") or entry.get("UNIT") or "")
    identifier = str(entry.get("SYSLOG_IDENTIFIER") or entry.get("_COMM") or unit or "")
    pid = str(entry.get("_PID") or entry.get("SYSLOG_PID") or "")
    message = _journal_message(entry.get("MESSAGE"))

    return {
        "timestamp": timestamp,
        "priority": priority,
        "level": _PRIORITY_NAMES[priority],
        "unit": unit,
        "identifier": identifier,
        "pid": pid,
        "message": message,
        "hostname": str(entry.get("_HOSTNAME") or ""),
        "transport": str(entry.get("_TRANSPORT") or ""),
        "cursor": str(entry.get("__CURSOR") or ""),
    }


def _journalctl_history_args(query: dict) -> List[str]:
    args = [
        "journalctl",
        "--no-pager",
        "--reverse",
        "--output=json",
        "--since",
        query["date_from"].strftime("%Y-%m-%d %H:%M:%S"),
        "--until",
        query["date_to"].strftime("%Y-%m-%d %H:%M:%S"),
    ]
    if query["level"] == "warning":
        args += ["--priority", "warning"]
    elif query["level"] == "error":
        args += ["--priority", "err"]
    for unit in query["units"]:
        args += ["--unit", unit]
    return args


def _read_journal_history(query: dict, *, include_all: bool = False) -> dict:
    try:
        completed = subprocess.run(
            _journalctl_history_args(query),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=HISTORY_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HistoryQueryError("サービスログの検索がタイムアウトしました。期間を短くしてください。") from exc
    except OSError as exc:
        raise HistoryQueryError("journalctlを実行できませんでした。") from exc

    if completed.returncode != 0:
        detail = (completed.stderr or "").strip()
        raise HistoryQueryError(detail or "サービスログを読み取れませんでした。")

    needle = query["keyword"].casefold()
    offset = (query["page"] - 1) * query["page_size"]
    end = offset + query["page_size"]
    total = 0
    rows: List[dict] = []
    all_rows: List[dict] = []

    for raw_line in completed.stdout.splitlines():
        if not raw_line.strip():
            continue
        try:
            decoded = _decode_journal_entry(json.loads(raw_line))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if needle:
            haystack = "\n".join(
                (
                    decoded["message"],
                    decoded["unit"],
                    decoded["identifier"],
                    decoded["pid"],
                    decoded["hostname"],
                )
            ).casefold()
            if needle not in haystack:
                continue
        if include_all:
            all_rows.append(decoded)
        if offset <= total < end:
            rows.append(decoded)
        total += 1

    pages = max(1, (total + query["page_size"] - 1) // query["page_size"])
    return {
        "rows": rows,
        "all_rows": all_rows,
        "total": total,
        "pages": pages,
        "page": query["page"],
        "page_size": query["page_size"],
    }


def _csv_safe(value: Any) -> str:
    text = str(value or "")
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text

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


@bp_service_logs.get("/history")
def history():
    try:
        query = _history_query_from_args(request.args)
        result = _read_journal_history(query)
    except HistoryQueryError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify(
        {
            "ok": True,
            "service": query["service"],
            "label": query["label"],
            "date_from": query["date_from"].isoformat(timespec="minutes"),
            "date_to": query["date_to"].isoformat(timespec="minutes"),
            "level": query["level"],
            "keyword": query["keyword"],
            "page": result["page"],
            "page_size": result["page_size"],
            "pages": result["pages"],
            "total": result["total"],
            "rows": result["rows"],
            "fetched_at": datetime.now(JST).isoformat(timespec="seconds"),
        }
    )


@bp_service_logs.get("/history.csv")
def history_csv():
    try:
        query = _history_query_from_args(request.args)
        result = _read_journal_history(query, include_all=True)
    except HistoryQueryError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if result["total"] > HISTORY_CSV_MAX_ROWS:
        return jsonify(
            {
                "ok": False,
                "error": f"CSVは最大{HISTORY_CSV_MAX_ROWS:,}件です。検索期間または条件を絞ってください。",
            }
        ), 413

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(("日時", "重要度", "サービス", "プロセス", "PID", "メッセージ", "ホスト", "カーソル"))
    for row in result["all_rows"]:
        writer.writerow(
            (
                _csv_safe(row["timestamp"]),
                _csv_safe(row["level"]),
                _csv_safe(row["unit"]),
                _csv_safe(row["identifier"]),
                _csv_safe(row["pid"]),
                _csv_safe(row["message"]),
                _csv_safe(row["hostname"]),
                _csv_safe(row["cursor"]),
            )
        )

    filename = (
        f"service_logs_{query['service']}_"
        f"{query['date_from'].strftime('%Y%m%d_%H%M')}-"
        f"{query['date_to'].strftime('%Y%m%d_%H%M')}.csv"
    )
    payload = "\ufeff" + output.getvalue()
    return Response(
        payload,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp_service_logs.get("/")
def page():
    units, labels, initial, default_lines = _get_config()
    history_capable = {
        key: not (isinstance(spec, dict) and "file" in spec)
        for key, spec in units.items()
    }
    return render_template(
        "service_logs.html",
        units=units,
        labels=labels,
        initial=initial,
        default_lines=default_lines,
        history_capable=history_capable,
        history_page_sizes=HISTORY_PAGE_SIZES,
        history_max_days=HISTORY_MAX_DAYS,
    )
