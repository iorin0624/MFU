import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import abort, current_app, flash, jsonify, make_response, redirect, render_template, request, send_file, session, url_for

from app.utils.db import get_db
from app.discord_notifications.repository import get_discord_webhook

from . import signage_admin_bp, signage_bp
from .rain_alert import run_rain_analysis


MAPS_KEY_SETTING = "GOOGLE_MAPS_BROWSER_API_KEY"
TRAFFIC_CONFIG_SETTING = "SIGNAGE_TRAFFIC_CONFIG"
TRAFFIC_VIEW_SETTING_PREFIX = "SIGNAGE_TRAFFIC_VIEW_"
MAX_TRAFFIC_VIEWS = 12
RIVER_CONFIG_SETTING = "SIGNAGE_RIVER_CONFIG"
RIVER_CAPTURE_PATH = os.environ.get("SIGNAGE_RIVER_CAPTURE_PATH", "/run/mfu-signage/river-latest.png")
RIVER_CAPTURE_STATUS_PATH = os.environ.get(
    "SIGNAGE_RIVER_CAPTURE_STATUS_PATH", "/run/mfu-signage/river-status.json"
)
RIVER_HISTORY_DIR = Path(
    os.environ.get("SIGNAGE_RIVER_HISTORY_DIR", "/mnt/mfu/signage_archive/river")
)
RIVER_HISTORY_FILE_RE = re.compile(r"^\d{8}T\d{6}[+-]\d{4}\.webp$")
RAIN_ALERT_CONFIG_SETTING = "SIGNAGE_RAIN_ALERT_CONFIG"
RAIN_ALERT_POINT_SETTING_PREFIX = "SIGNAGE_RAIN_ALERT_POINT_"
RAIN_ALERT_STATUS_PATH = Path(
    os.environ.get("SIGNAGE_RAIN_ALERT_STATUS_PATH", "/run/mfu-signage/rain-alert-status.json")
)
RAIN_ALERT_STATE_PATH = Path(
    os.environ.get(
        "SIGNAGE_RAIN_ALERT_STATE_PATH",
        "/mnt/mfu/signage_archive/river/.rain-alert-state.json",
    )
)
PUBLIC_BASE_URL = os.environ.get("MFU_PUBLIC_BASE_URL", "https://mfu.iori0624.jp").rstrip("/")
DEFAULT_RIVER_URL = (
    "https://www.river.go.jp/kawabou/pc/ov?"
    "zm=11&clat=35.53194683501495&clon=139.85321044921878&fld=0&mapType=0&"
    "viewGrpStg=0&viewRd=1&viewRW=1&viewRiver=1&viewPoint=1&viewRl=1&viewRn=1"
)
DEFAULT_RIVER_CONFIG = {
    "enabled": True,
    "url": DEFAULT_RIVER_URL,
    "display_seconds": 15,
}
DEFAULT_RAIN_ALERT_CONFIG = {
    "enabled": False,
    "confirmations": 3,
    "points": [
        {
            "id": "point-1",
            "enabled": False,
            "name": "地点1",
            "lat": 35.5319468,
            "lng": 139.8532104,
            "notify_start": "07:00",
            "notify_end": "23:00",
            "rain_minutes": 30,
            "stop_minutes": 20,
        },
        {
            "id": "point-2",
            "enabled": False,
            "name": "地点2",
            "lat": 35.5129458,
            "lng": 140.0166321,
            "notify_start": "07:00",
            "notify_end": "23:00",
            "rain_minutes": 30,
            "stop_minutes": 20,
        },
    ],
}
TIME_VALUE_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
MAPS_KEY_RE = re.compile(r"^AIza[0-9A-Za-z_-]{30,}$")
MAPS_VIEW_URL_RE = re.compile(
    r"@(?P<lat>-?\d+(?:\.\d+)?),(?P<lng>-?\d+(?:\.\d+)?),(?P<zoom>\d+(?:\.\d+)?)z"
)
DEFAULT_TRAFFIC_CONFIG = {
    "views": [
        {
            "name": "千葉・湾岸広域",
            "map_url": "https://www.google.co.jp/maps/@35.5374273,139.8837669,11.96z/data=!5m1!1e1",
            "center": {"lat": 35.5374273, "lng": 139.8837669},
            "zoom": 11.96,
            "duration_seconds": 20,
        },
        {
            "name": "首都高・外環道",
            "map_url": "https://www.google.co.jp/maps/@35.716951,139.7222709,11.96z/data=!5m1!1e1",
            "center": {"lat": 35.716951, "lng": 139.7222709},
            "zoom": 11.96,
            "duration_seconds": 20,
        },
    ]
}


def _validated_river_config(value):
    source = value if isinstance(value, dict) else {}
    enabled = str(source.get("enabled", "1")).strip().lower() in {
        "1", "true", "yes", "on"
    }
    river_url = str(source.get("url") or DEFAULT_RIVER_URL).strip()
    parsed = urlparse(river_url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in {"river.go.jp", "www.river.go.jp"}
        or not parsed.path.startswith("/kawabou/pc/")
    ):
        raise ValueError("川の防災情報URLはriver.go.jpのPC版URLを指定してください。")
    try:
        display_seconds = int(source.get("display_seconds", 15))
    except (TypeError, ValueError):
        raise ValueError("川情報の表示時間は整数で指定してください。")
    if not 5 <= display_seconds <= 180:
        raise ValueError("川情報の表示時間は5～180秒で指定してください。")
    return {
        "enabled": enabled,
        "url": river_url,
        "display_seconds": display_seconds,
    }


def _river_config():
    db = get_db()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            "SELECT `value` FROM settings WHERE `key`=%s LIMIT 1",
            (RIVER_CONFIG_SETTING,),
        )
        row = cursor.fetchone()
    finally:
        db.close()
    if not row or not row.get("value"):
        return dict(DEFAULT_RIVER_CONFIG)
    try:
        return _validated_river_config(json.loads(str(row["value"])))
    except (TypeError, ValueError, json.JSONDecodeError):
        current_app.logger.warning("SIGNAGE_RIVER_CONFIG_INVALID using_defaults=1")
        return dict(DEFAULT_RIVER_CONFIG)


def _validated_rain_alert_config(value):
    source = value if isinstance(value, dict) else {}
    points_source = source.get("points") if isinstance(source.get("points"), list) else []
    points = []
    for index in range(2):
        default = DEFAULT_RAIN_ALERT_CONFIG["points"][index]
        raw = points_source[index] if index < len(points_source) and isinstance(points_source[index], dict) else {}
        name = str(raw.get("name") or default["name"]).strip()
        if not name or len(name) > 40:
            raise ValueError(f"地点{index + 1}の名前は1～40文字で入力してください。")
        try:
            lat = float(raw.get("lat", default["lat"]))
            lng = float(raw.get("lng", default["lng"]))
            rain_minutes = int(raw.get("rain_minutes", 30))
            stop_minutes = int(raw.get("stop_minutes", 20))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"地点{index + 1}の座標または判定時間が不正です。") from exc
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError(f"地点{index + 1}の緯度・経度が範囲外です。")
        if not 5 <= rain_minutes <= 120:
            raise ValueError(f"地点{index + 1}の「降りそう」は5～120分で指定してください。")
        if not 5 <= stop_minutes <= 120:
            raise ValueError(f"地点{index + 1}の「止みそう」は5～120分で指定してください。")
        notify_start = str(raw.get("notify_start") or default["notify_start"]).strip()
        notify_end = str(raw.get("notify_end") or default["notify_end"]).strip()
        if not TIME_VALUE_RE.fullmatch(notify_start) or not TIME_VALUE_RE.fullmatch(notify_end):
            raise ValueError(f"地点{index + 1}の通知時間帯が不正です。")
        points.append(
            {
                "id": f"point-{index + 1}",
                "enabled": str(raw.get("enabled", "0")).strip().lower() in {"1", "true", "yes", "on"},
                "name": name,
                "lat": round(lat, 7),
                "lng": round(lng, 7),
                "notify_start": notify_start,
                "notify_end": notify_end,
                "rain_minutes": rain_minutes,
                "stop_minutes": stop_minutes,
            }
        )
    return {
        "enabled": str(source.get("enabled", "0")).strip().lower() in {"1", "true", "yes", "on"},
        "confirmations": 3,
        "points": points,
    }


def _rain_alert_config():
    db = get_db()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT `key`, `value`
              FROM settings
             WHERE LEFT(`key`, %s)=%s
             ORDER BY `key`
            """,
            (len(RAIN_ALERT_POINT_SETTING_PREFIX), RAIN_ALERT_POINT_SETTING_PREFIX),
        )
        point_rows = cursor.fetchall()
        cursor.execute(
            "SELECT `value` FROM settings WHERE `key`=%s LIMIT 1",
            (RAIN_ALERT_CONFIG_SETTING,),
        )
        row = cursor.fetchone()
    finally:
        db.close()
    if point_rows:
        try:
            root = json.loads(str((row or {}).get("value") or "{}"))
            points = []
            for index, point_row in enumerate(point_rows[:2]):
                values = json.loads(str(point_row.get("value") or "{}"))
                points.append(
                    {
                        "enabled": values.get("e", 0),
                        "name": values.get("n"),
                        "lat": values.get("a"),
                        "lng": values.get("g"),
                        "notify_start": values.get("s"),
                        "notify_end": values.get("t"),
                        "rain_minutes": values.get("r"),
                        "stop_minutes": values.get("x"),
                    }
                )
            while len(points) < 2:
                points.append(dict(DEFAULT_RAIN_ALERT_CONFIG["points"][len(points)]))
            return _validated_rain_alert_config(
                {"enabled": root.get("e", 0), "points": points}
            )
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            current_app.logger.warning("SIGNAGE_RAIN_ALERT_POINT_CONFIG_INVALID using_defaults=1")
    if not row or not row.get("value"):
        return json.loads(json.dumps(DEFAULT_RAIN_ALERT_CONFIG, ensure_ascii=False))
    try:
        return _validated_rain_alert_config(json.loads(str(row["value"])))
    except (TypeError, ValueError, json.JSONDecodeError):
        current_app.logger.warning("SIGNAGE_RAIN_ALERT_CONFIG_INVALID using_defaults=1")
        return json.loads(json.dumps(DEFAULT_RAIN_ALERT_CONFIG, ensure_ascii=False))


def _admin_discord_webhook():
    db = get_db()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT webhook_url
              FROM users
             WHERE username=%s
               AND webhook_url IS NOT NULL
               AND webhook_url <> ''
             LIMIT 1
            """,
            ("admin",),
        )
        row = cursor.fetchone()
        legacy = str((row or {}).get("webhook_url") or "").strip()
        return get_discord_webhook("rain_alert", legacy)
    finally:
        db.close()


def _maps_api_key():
    env_key = os.environ.get(MAPS_KEY_SETTING, "").strip()
    if env_key:
        return env_key
    db = get_db()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("SELECT `value` FROM settings WHERE `key`=%s LIMIT 1", (MAPS_KEY_SETTING,))
        row = cursor.fetchone()
        return str((row or {}).get("value") or "").strip()
    finally:
        db.close()


def _traffic_config():
    config = json.loads(json.dumps(DEFAULT_TRAFFIC_CONFIG, ensure_ascii=False))
    db = get_db()
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT `key`, `value`
              FROM settings
             WHERE LEFT(`key`, %s)=%s
             ORDER BY `key`
            """,
            (len(TRAFFIC_VIEW_SETTING_PREFIX), TRAFFIC_VIEW_SETTING_PREFIX),
        )
        view_rows = cursor.fetchall()
        cursor.execute(
            "SELECT `value` FROM settings WHERE `key`=%s LIMIT 1",
            (TRAFFIC_CONFIG_SETTING,),
        )
        row = cursor.fetchone()
    finally:
        db.close()

    if view_rows:
        views = []
        for index, view_row in enumerate(view_rows[:MAX_TRAFFIC_VIEWS]):
            try:
                values = json.loads(str(view_row.get("value") or ""))
                views.append(
                    _validated_view(
                        {
                            "name": values.get("n"),
                            "center": {"lat": values.get("a"), "lng": values.get("g")},
                            "zoom": values.get("z"),
                            "duration_seconds": values.get("d"),
                        },
                        index,
                    )
                )
            except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
                current_app.logger.warning(
                    "SIGNAGE_TRAFFIC_VIEW_INVALID key=%s",
                    view_row.get("key"),
                )
        if views:
            return {"views": views}

    if not row or not row.get("value"):
        return config
    try:
        stored = json.loads(str(row["value"]))
        compact_views = stored.get("v") if isinstance(stored, dict) else None
        if isinstance(compact_views, list) and compact_views:
            views = []
            for index, values in enumerate(compact_views):
                if not isinstance(values, list) or len(values) != 4:
                    raise ValueError
                lat, lng, zoom, duration = values
                default = _default_view(index)
                views.append(
                    _validated_view(
                        {
                            "name": default["name"],
                            "map_url": f"https://www.google.co.jp/maps/@{lat},{lng},{zoom}z/data=!5m1!1e1",
                            "center": {"lat": lat, "lng": lng},
                            "zoom": zoom,
                            "duration_seconds": duration,
                        },
                        index,
                    )
                )
            return {"views": views}
        views = stored.get("views") if isinstance(stored, dict) else None
        if not isinstance(views, list) or not views:
            return config
        return {
            "views": [
                _validated_view(view, index)
                for index, view in enumerate(views[:MAX_TRAFFIC_VIEWS])
            ]
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        current_app.logger.warning("SIGNAGE_TRAFFIC_CONFIG_INVALID using_defaults=1")
        return config


def _default_view(index):
    defaults = DEFAULT_TRAFFIC_CONFIG["views"]
    if index < len(defaults):
        return defaults[index]
    return {
        "name": f"地図{index + 1}",
        "map_url": defaults[-1]["map_url"],
        "center": dict(defaults[-1]["center"]),
        "zoom": defaults[-1]["zoom"],
        "duration_seconds": defaults[-1]["duration_seconds"],
    }


def _validated_view(view, index):
    default = _default_view(index)
    center = view.get("center") if isinstance(view, dict) else None
    try:
        lat = float((center or {}).get("lat"))
        lng = float((center or {}).get("lng"))
        zoom = float(view.get("zoom"))
        duration = int(view.get("duration_seconds"))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180 and 5 <= zoom <= 20 and 5 <= duration <= 180):
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        return dict(default)
    name = str(view.get("name") or default["name"]).strip()[:80]
    return {
        "name": name or default["name"],
        "map_url": str(
            view.get("map_url")
            or f"https://www.google.co.jp/maps/@{lat},{lng},{zoom}z/data=!5m1!1e1"
        ),
        "center": {"lat": lat, "lng": lng},
        "zoom": zoom,
        "duration_seconds": duration,
    }


def _view_from_form(name, map_url, duration):
    name = str(name or "").strip()
    if not name:
        raise ValueError("地図タイトルを入力してください。")
    if len(name) > 80:
        raise ValueError("地図タイトルは80文字以内で入力してください。")
    match = MAPS_VIEW_URL_RE.search(map_url)
    if not match:
        raise ValueError("GoogleマップURLから座標とズームを読み取れません。")
    lat = float(match.group("lat"))
    lng = float(match.group("lng"))
    zoom = float(match.group("zoom"))
    duration = int(duration)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180 and 5 <= zoom <= 20):
        raise ValueError("地図の座標またはズームが範囲外です。")
    if not 5 <= duration <= 180:
        raise ValueError("表示秒数は5～180秒で指定してください。")
    return {
        "name": name,
        "map_url": map_url,
        "center": {"lat": lat, "lng": lng},
        "zoom": zoom,
        "duration_seconds": duration,
    }


@signage_bp.get("/traffic")
def traffic():
    """Public, read-only traffic display for the on-premise digital signage."""
    api_key = _maps_api_key()
    response = make_response(
        render_template(
            "signage/traffic.html",
            google_maps_api_key=api_key,
            traffic_config=_traffic_config(),
        )
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    # A browser-restricted Maps key needs the MFU origin as Referer.
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not api_key:
        current_app.logger.warning(
            "Traffic signage is waiting for GOOGLE_MAPS_BROWSER_API_KEY"
        )
    return response


@signage_bp.get("/traffic/health")
def traffic_health():
    """Key-free readiness probe used by the Raspberry Pi controller."""
    configured = bool(_maps_api_key())
    config = _traffic_config()
    display_seconds = sum(view["duration_seconds"] for view in config["views"])
    response = jsonify(
        {
            "ok": True,
            "configured": configured,
            "display_url": "/signage/traffic",
            "display_seconds": display_seconds,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@signage_bp.get("/traffic/config")
def traffic_config():
    config = _traffic_config()
    config["display_seconds"] = sum(view["duration_seconds"] for view in config["views"])
    response = jsonify(config)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@signage_bp.get("/river/health")
def river_health():
    """Read-only river display configuration for the Raspberry Pi controller."""
    config = _river_config()
    response = jsonify({"ok": True, **config})
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@signage_bp.get("/river/image")
def river_image():
    if not os.path.isfile(RIVER_CAPTURE_PATH):
        abort(503, description="河川情報画像を準備中です。")
    response = make_response(
        send_file(
            RIVER_CAPTURE_PATH,
            mimetype="image/png",
            conditional=True,
            etag=True,
            max_age=0,
        )
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@signage_bp.get("/river/image/status")
def river_image_status():
    status = {"ok": False, "message": "河川情報画像を準備中です。"}
    try:
        with open(RIVER_CAPTURE_STATUS_PATH, "r", encoding="utf-8") as status_file:
            loaded = json.load(status_file)
        if isinstance(loaded, dict):
            status.update(loaded)
    except (FileNotFoundError, OSError, ValueError):
        pass
    if os.path.isfile(RIVER_CAPTURE_PATH):
        import time

        mtime = os.path.getmtime(RIVER_CAPTURE_PATH)
        status["image_available"] = True
        status["image_age_seconds"] = max(0, int(time.time() - mtime))
        status["image_size_bytes"] = os.path.getsize(RIVER_CAPTURE_PATH)
    else:
        status["image_available"] = False
    response = jsonify(status)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@signage_bp.post("/river/rain/analyze")
def river_rain_analyze():
    """Run after a successful capture. This endpoint is loopback-only."""
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        abort(404)
    river_config = _river_config()
    result = run_rain_analysis(
        config=_rain_alert_config(),
        river_url=river_config["url"],
        history_dir=RIVER_HISTORY_DIR,
        latest_path=Path(RIVER_CAPTURE_PATH),
        state_path=RAIN_ALERT_STATE_PATH,
        status_path=RAIN_ALERT_STATUS_PATH,
        webhook_url=_admin_discord_webhook(),
        page_url=f"{PUBLIC_BASE_URL}/admin/signage/traffic",
        logger=current_app.logger,
    )
    response = jsonify({"ok": True, **result})
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


def _require_signage_admin():
    if session.get("user") != "admin":
        abort(404)


def _river_history_entries():
    if not RIVER_HISTORY_DIR.is_dir():
        return []
    entries = []
    for path in sorted(RIVER_HISTORY_DIR.glob("*.webp")):
        if not RIVER_HISTORY_FILE_RE.fullmatch(path.name):
            continue
        try:
            captured_at = datetime.strptime(
                path.stem, "%Y%m%dT%H%M%S%z"
            ).isoformat(timespec="seconds")
            stat = path.stat()
        except (OSError, ValueError):
            continue
        entries.append(
            {
                "filename": path.name,
                "captured_at": captured_at,
                "size_bytes": stat.st_size,
                "url": url_for(
                    "signage_admin.river_history_image",
                    filename=path.name,
                ),
            }
        )
    return entries


@signage_admin_bp.get("/river-history-viewer")
def river_history_viewer():
    _require_signage_admin()
    response = make_response(
        render_template(
            "signage/river_history_viewer.html",
            river_config=_river_config(),
            rain_alert_config=_rain_alert_config(),
        )
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@signage_admin_bp.get("/traffic/river-history")
def river_history():
    _require_signage_admin()
    entries = _river_history_entries()
    response = jsonify(
        {
            "ok": True,
            "entries": entries,
            "count": len(entries),
            "fine_interval_seconds": 60,
            "fine_retention_minutes": 30,
            "coarse_interval_seconds": 300,
            "retention_hours": 96,
        }
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@signage_admin_bp.get("/traffic/rain-alert-status")
def rain_alert_status():
    _require_signage_admin()
    config = _rain_alert_config()
    status = {
        "updated_at": None,
        "enabled": bool(config.get("enabled")),
        "discord_configured": bool(_admin_discord_webhook()),
        "points": [],
    }
    try:
        loaded = json.loads(RAIN_ALERT_STATUS_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            status.update(loaded)
    except (FileNotFoundError, OSError, ValueError):
        pass

    # The saved point results are the last successful analysis.  A transient
    # analyzer failure must not erase those results or make the UI claim that
    # the feature was disabled by configuration.
    status["enabled"] = bool(config.get("enabled"))
    status["last_success_at"] = status.get("updated_at")
    capture_status = {}
    try:
        loaded = json.loads(Path(RIVER_CAPTURE_STATUS_PATH).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            capture_status = loaded
    except (FileNotFoundError, OSError, ValueError):
        pass

    status["last_attempt_at"] = capture_status.get("updated_at")
    status["analysis_error"] = capture_status.get("rain_analysis_error")
    if not status["enabled"]:
        status["health_state"] = "disabled"
    elif capture_status.get("rain_analysis_ok") is False:
        status["health_state"] = "error"
    elif status.get("points"):
        status["health_state"] = "ok"
    else:
        status["health_state"] = "pending"
    response = jsonify({"ok": True, **status})
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@signage_admin_bp.get("/traffic/river-history/<filename>")
def river_history_image(filename):
    _require_signage_admin()
    if not RIVER_HISTORY_FILE_RE.fullmatch(str(filename or "")):
        abort(404)
    path = RIVER_HISTORY_DIR / filename
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(RIVER_HISTORY_DIR.resolve())
    except (FileNotFoundError, OSError, ValueError):
        abort(404)
    if not resolved.is_file():
        abort(404)
    response = make_response(
        send_file(
            resolved,
            mimetype="image/webp",
            conditional=True,
            etag=True,
            max_age=300,
        )
    )
    response.headers["Cache-Control"] = "private, max-age=300"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@signage_admin_bp.route("/traffic", methods=["GET", "POST"])
def traffic_settings():
    if session.get("user") != "admin":
        abort(404)
    csrf_token = str(session.get("csrf_token") or "")
    if not csrf_token:
        csrf_token = secrets.token_urlsafe(32)
        session["csrf_token"] = csrf_token

    if request.method == "POST":
        submitted_csrf = str(request.form.get("csrf_token") or "")
        if not submitted_csrf or not secrets.compare_digest(csrf_token, submitted_csrf):
            abort(400, "CSRF token mismatch")
        action = str(request.form.get("action") or "")
        if action == "save_api_key":
            api_key = str(request.form.get("google_maps_api_key") or "").strip()
            if not MAPS_KEY_RE.fullmatch(api_key):
                flash("Google Maps APIキーの形式が正しくありません。", "danger")
                return redirect(url_for("signage_admin.traffic_settings"))
            setting_key = MAPS_KEY_SETTING
            setting_value = api_key
            log_message = "SIGNAGE_TRAFFIC_API_KEY_UPDATED actor=admin"
            success_message = "Google Maps APIキーを保存しました。"
        elif action == "save_traffic_config":
            try:
                names = request.form.getlist("view_name")
                map_urls = request.form.getlist("map_url")
                durations = request.form.getlist("duration_seconds")
                if not (len(names) == len(map_urls) == len(durations)):
                    raise ValueError("地図設定の送信内容が不正です。")
                if not 1 <= len(names) <= MAX_TRAFFIC_VIEWS:
                    raise ValueError(f"地図は1～{MAX_TRAFFIC_VIEWS}件で設定してください。")
                config = {
                    "views": [
                        _view_from_form(name, map_url.strip(), duration)
                        for name, map_url, duration in zip(names, map_urls, durations)
                    ]
                }
            except (TypeError, ValueError) as exc:
                flash(str(exc), "danger")
                return redirect(url_for("signage_admin.traffic_settings"))
            db = get_db()
            try:
                cursor = db.cursor()
                cursor.execute(
                    "DELETE FROM settings WHERE LEFT(`key`, %s)=%s",
                    (len(TRAFFIC_VIEW_SETTING_PREFIX), TRAFFIC_VIEW_SETTING_PREFIX),
                )
                cursor.executemany(
                    "INSERT INTO settings (`key`, `value`) VALUES (%s, %s)",
                    [
                        (
                            f"{TRAFFIC_VIEW_SETTING_PREFIX}{index:03d}",
                            json.dumps(
                                {
                                    "n": view["name"],
                                    "a": view["center"]["lat"],
                                    "g": view["center"]["lng"],
                                    "z": view["zoom"],
                                    "d": view["duration_seconds"],
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                        for index, view in enumerate(config["views"], start=1)
                    ],
                )
                db.commit()
            finally:
                db.close()
            current_app.logger.info(
                "SIGNAGE_TRAFFIC_CONFIG_UPDATED actor=admin view_count=%s",
                len(config["views"]),
            )
            flash("地図の順番・タイトル・表示秒数を保存しました。次回表示から反映されます。", "success")
            return redirect(url_for("signage_admin.traffic_settings"))
        elif action == "save_river_config":
            try:
                config = _validated_river_config(
                    {
                        "enabled": request.form.get("enabled") or "0",
                        "url": request.form.get("river_url"),
                        "display_seconds": request.form.get("display_seconds"),
                    }
                )
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("signage_admin.traffic_settings"))
            setting_key = RIVER_CONFIG_SETTING
            setting_value = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
            log_message = (
                "SIGNAGE_RIVER_CONFIG_UPDATED actor=admin "
                f"enabled={int(config['enabled'])} display_seconds={config['display_seconds']}"
            )
            success_message = "川の防災情報サイネージ設定を保存しました。"
        elif action == "save_rain_alert_config":
            try:
                points = []
                for index in range(2):
                    points.append(
                        {
                            "enabled": request.form.get(f"point_enabled_{index}") or "0",
                            "name": request.form.get(f"point_name_{index}"),
                            "lat": request.form.get(f"point_lat_{index}"),
                            "lng": request.form.get(f"point_lng_{index}"),
                            "notify_start": request.form.get(f"point_notify_start_{index}"),
                            "notify_end": request.form.get(f"point_notify_end_{index}"),
                            "rain_minutes": request.form.get(f"point_rain_minutes_{index}"),
                            "stop_minutes": request.form.get(f"point_stop_minutes_{index}"),
                        }
                    )
                config = _validated_rain_alert_config(
                    {
                        "enabled": request.form.get("rain_alert_enabled") or "0",
                        "points": points,
                    }
                )
            except ValueError as exc:
                flash(str(exc), "danger")
                return redirect(url_for("signage_admin.traffic_settings"))
            db = get_db()
            try:
                cursor = db.cursor()
                cursor.execute(
                    "DELETE FROM settings WHERE LEFT(`key`, %s)=%s",
                    (len(RAIN_ALERT_POINT_SETTING_PREFIX), RAIN_ALERT_POINT_SETTING_PREFIX),
                )
                cursor.executemany(
                    "INSERT INTO settings (`key`, `value`) VALUES (%s, %s)",
                    [
                        (
                            f"{RAIN_ALERT_POINT_SETTING_PREFIX}{index:03d}",
                            json.dumps(
                                {
                                    "e": int(point["enabled"]),
                                    "n": point["name"],
                                    "a": point["lat"],
                                    "g": point["lng"],
                                    "s": point["notify_start"],
                                    "t": point["notify_end"],
                                    "r": point["rain_minutes"],
                                    "x": point["stop_minutes"],
                                },
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                        for index, point in enumerate(config["points"], start=1)
                    ],
                )
                cursor.execute(
                    """
                    INSERT INTO settings (`key`, `value`) VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)
                    """,
                    (
                        RAIN_ALERT_CONFIG_SETTING,
                        json.dumps({"e": int(config["enabled"])}, separators=(",", ":")),
                    ),
                )
                db.commit()
            finally:
                db.close()
            RAIN_ALERT_STATE_PATH.unlink(missing_ok=True)
            RAIN_ALERT_STATUS_PATH.unlink(missing_ok=True)
            current_app.logger.info(
                "SIGNAGE_RAIN_ALERT_CONFIG_UPDATED actor=admin "
                f"enabled={int(config['enabled'])} "
                f"point_count={sum(1 for point in config['points'] if point['enabled'])}"
            )
            flash("雨雲接近・雨上がり通知設定を保存しました。次回の毎分取得から反映されます。", "success")
            return redirect(url_for("signage_admin.traffic_settings"))
        else:
            abort(400, "Unknown action")
        db = get_db()
        try:
            cursor = db.cursor()
            cursor.execute(
                """
                INSERT INTO settings (`key`, `value`) VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE `value`=VALUES(`value`)
                """,
                (setting_key, setting_value),
            )
            db.commit()
        finally:
            db.close()
        if setting_key == RAIN_ALERT_CONFIG_SETTING:
            RAIN_ALERT_STATE_PATH.unlink(missing_ok=True)
            RAIN_ALERT_STATUS_PATH.unlink(missing_ok=True)
        current_app.logger.info(log_message)
        flash(success_message, "success")
        return redirect(url_for("signage_admin.traffic_settings"))

    configured = bool(_maps_api_key())
    config = _traffic_config()
    return render_template(
        "signage/traffic_settings.html",
        configured=configured,
        traffic_config=config,
        river_config=_river_config(),
        rain_alert_config=_rain_alert_config(),
        discord_configured=bool(_admin_discord_webhook()),
        csrf_token_value=csrf_token,
    )
