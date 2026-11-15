"""First-party analytics for the public pro/suc hostnames only."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

JST = timezone(timedelta(hours=9))
HOST_SITE = {"pro.iori0624.jp": "pro", "suc.iori0624.jp": "suc"}
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False
_BOT_RE = re.compile(r"bot|spider|crawler|slurp|preview|headless|curl|wget|python-requests|facebookexternalhit", re.I)
_SOURCE_RE = re.compile(r'\[TRAFFIC_SOURCE\]\s+src=("(?:\\.|[^"\\])*")')


def normalize_src(value: object | None) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:80]


def referrer_host(value: str | None) -> str:
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in {"http", "https"}:
            return ""
        return str(parts.hostname or "").lower()[:255]
    except ValueError:
        return ""


def classify_ua(value: str | None) -> tuple[str, str, str, bool]:
    ua = str(value or "")
    bot = bool(_BOT_RE.search(ua)) or not ua.strip()
    if bot:
        return "Bot", "不明", "不明", True
    if "iPhone" in ua or "iPod" in ua:
        device, os_name = "スマートフォン", "iOS"
    elif "iPad" in ua:
        device, os_name = "タブレット", "iPadOS"
    elif "Android" in ua:
        device, os_name = ("スマートフォン" if "Mobile" in ua else "タブレット"), "Android"
    elif "Windows" in ua:
        device, os_name = "PC", "Windows"
    elif "CrOS" in ua:
        device, os_name = "PC", "ChromeOS"
    elif "Mac OS X" in ua or "Macintosh" in ua:
        device, os_name = "PC", "macOS"
    elif "Linux" in ua:
        device, os_name = "PC", "Linux"
    else:
        device, os_name = "その他", "不明"
    if "GSA/" in ua:
        browser = "Googleアプリ"
    elif "Instagram" in ua:
        browser = "Instagram内ブラウザー"
    elif "Line/" in ua or "LINE/" in ua:
        browser = "LINE内ブラウザー"
    elif "Edg/" in ua or "EdgiOS/" in ua:
        browser = "Edge"
    elif "FxiOS/" in ua or "Firefox/" in ua:
        browser = "Firefox"
    elif "CriOS/" in ua or "Chrome/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua:
        browser = "Safari"
    else:
        browser = "その他"
    return device, os_name, browser, False


def viewed_calendar_month(query: dict, when: datetime) -> str:
    try:
        year = int((query.get("year") or [when.year])[0])
        month = int((query.get("month") or [when.month])[0])
        year += (month - 1) // 12
        month = (month - 1) % 12 + 1
        if 1 <= year <= 9999:
            return f"{year:04d}-{month:02d}"
    except (TypeError, ValueError, IndexError):
        pass
    return f"{when.year:04d}-{when.month:02d}"


def make_event(
    *, site: str, viewed_at: datetime, ip: str, ua: str, referer: str,
    src: str = "", src_explicit: bool = False, query: dict | None = None,
    origin: str = "live", legacy_key: str | None = None,
) -> dict:
    host = referrer_host(referer)
    normalized_src = normalize_src(src)
    if normalized_src:
        kind, label = "src", normalized_src
    elif host and host not in HOST_SITE:
        kind, label = "referrer", host
    elif host and host != next((key for key, value in HOST_SITE.items() if value == site), ""):
        kind, label = "referrer", host
    else:
        kind, label = "direct", "直接・不明"
    device, os_name, browser, bot = classify_ua(ua)
    return {
        "site": site, "viewed_at": viewed_at.replace(tzinfo=None),
        "src": normalized_src, "src_explicit": int(src_explicit),
        "referer_host": host, "source_kind": kind, "source_label": label,
        "ip": str(ip or "-")[:64],
        "device": device, "os_name": os_name, "browser": browser,
        "calendar_month": viewed_calendar_month(query or {}, viewed_at) if site == "suc" and origin != "legacy_db" else None,
        "is_bot": int(bot), "origin": origin, "legacy_key": legacy_key,
    }


def event_for_request(request, response, session, marker: str = "") -> dict | None:
    host = str(request.host or "").partition(":")[0].rstrip(".").lower()
    site = HOST_SITE.get(host)
    if not site or request.method != "GET" or response.status_code != 200:
        return None
    if request.path != ("/profile" if site == "pro" else "/suc/"):
        return None
    if "[INAPP_WARNING]" in marker or "[EVENT_ALBUM_PREVIEW_BLOCKED]" in marker:
        return None
    raw_src = request.args.get("src")
    src = normalize_src(raw_src if raw_src is not None else session.get("mfu_public_traffic_source"))
    return make_event(
        site=site, viewed_at=datetime.now(JST), ip=request.remote_addr or "-",
        ua=request.headers.get("User-Agent", ""), referer=request.headers.get("Referer", ""),
        src=src, src_explicit=raw_src is not None,
        query={key: request.args.getlist(key) for key in ("year", "month")},
    )


def ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        from app.utils.db import get_db
        db = get_db()
        try:
            cur = db.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pro_suc_page_views (
                    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    viewed_at DATETIME NOT NULL,
                    site VARCHAR(8) NOT NULL,
                    src VARCHAR(80) NOT NULL DEFAULT '',
                    src_explicit TINYINT(1) NOT NULL DEFAULT 0,
                    referer_host VARCHAR(255) NOT NULL DEFAULT '',
                    source_kind VARCHAR(16) NOT NULL,
                    source_label VARCHAR(255) NOT NULL,
                    ip VARCHAR(64) NOT NULL,
                    device VARCHAR(32) NOT NULL,
                    os_name VARCHAR(32) NOT NULL,
                    browser VARCHAR(40) NOT NULL,
                    calendar_month CHAR(7) NULL,
                    is_bot TINYINT(1) NOT NULL DEFAULT 0,
                    origin VARCHAR(16) NOT NULL,
                    legacy_key VARCHAR(80) NULL,
                    INDEX idx_pro_suc_site_date (site, viewed_at),
                    INDEX idx_pro_suc_date (viewed_at),
                    UNIQUE KEY uq_pro_suc_legacy (origin, legacy_key)
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
            """)
            db.commit()
            _SCHEMA_READY = True
        finally:
            db.close()


_INSERT_SQL = """
    INSERT INTO pro_suc_page_views
      (viewed_at, site, src, src_explicit, referer_host, source_kind, source_label,
       ip, device, os_name, browser, calendar_month, is_bot, origin, legacy_key)
    VALUES (%(viewed_at)s, %(site)s, %(src)s, %(src_explicit)s, %(referer_host)s,
            %(source_kind)s, %(source_label)s, %(ip)s, %(device)s,
            %(os_name)s, %(browser)s, %(calendar_month)s, %(is_bot)s,
            %(origin)s, %(legacy_key)s)
    ON DUPLICATE KEY UPDATE id=id
"""


def insert_event(event: dict) -> None:
    ensure_schema()
    from app.utils.db import get_db
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute(_INSERT_SQL, event)
        db.commit()
    finally:
        db.close()


def ensure_nav_item() -> None:
    ensure_schema()
    from app.utils.db import get_db
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        cur.execute("SELECT id FROM mfu_nav_items WHERE url=%s LIMIT 1", ("/admin/pro_suc_pv",))
        if cur.fetchone():
            return
        cur.execute("SELECT id FROM mfu_nav_items WHERE parent_id IS NULL AND label LIKE %s ORDER BY id LIMIT 1", ("%システム%",))
        parent = cur.fetchone()
        parent_id = int(parent["id"]) if parent else None
        cur.execute("SELECT COALESCE(MAX(order_no),0) AS max_order FROM mfu_nav_items WHERE parent_id <=> %s", (parent_id,))
        order_no = int((cur.fetchone() or {}).get("max_order") or 0) + 10
        cur.execute("""
            INSERT INTO mfu_nav_items
              (parent_id,label,url,order_no,is_enabled,feature_key,open_in_new_tab,is_external)
            VALUES (%s,%s,%s,%s,1,'admin_pro_suc_pv',0,0)
        """, (parent_id, "pro・SUC PV分析", "/admin/pro_suc_pv", order_no))
        db.commit()
    finally:
        db.close()


def legacy_source(log_text: str | None) -> str:
    match = _SOURCE_RE.search(str(log_text or ""))
    if not match:
        return ""
    try:
        return normalize_src(json.loads(match.group(1)))
    except (ValueError, TypeError):
        return ""


def legacy_key_for_apache(line: str) -> str:
    return "apache:" + hashlib.sha256(line.encode("utf-8", errors="replace")).hexdigest()[:48]


def parse_apache_page_view(line: str, site: str) -> dict | None:
    match = re.search(r'^(?:\S+\s+)?(\S+)\s+\S+\s+\S+\s+\[([^]]+)\]\s+"GET\s+(\S+)\s+HTTP/[^\"]+"\s+(\d{3})\s+\S+\s+"([^"]*)"\s+"([^"]*)"', line)
    if not match:
        return None
    ip, stamp, target, status, referer, ua = match.groups()
    if status != "200":
        return None
    try:
        parts = urlsplit(target)
        viewed_at = datetime.strptime(stamp, "%d/%b/%Y:%H:%M:%S %z").astimezone(JST)
    except ValueError:
        return None
    if parts.path != "/":
        return None
    query = parse_qs(parts.query, keep_blank_values=True)
    src = normalize_src((query.get("src") or [""])[0])
    return make_event(
        site=site, viewed_at=viewed_at, ip=ip, ua=ua, referer=referer,
        src=src, src_explicit="src" in query, query=query,
        origin="legacy_apache", legacy_key=legacy_key_for_apache(line),
    )


def summary(start: date, end: date, site: str = "all", include_bots: bool = False) -> dict:
    ensure_schema()
    from app.utils.db import get_db
    from app.utils.local_networks import sql_local_ip_predicate
    db = get_db()
    try:
        cur = db.cursor(dictionary=True)
        local_ip_sql, local_ip_values = sql_local_ip_predicate("ip")
        where = ["viewed_at >= %s", "viewed_at < %s", f"NOT {local_ip_sql}"]
        params: list = [datetime.combine(start, datetime.min.time()), datetime.combine(end + timedelta(days=1), datetime.min.time())]
        params.extend(local_ip_values)
        if site in {"pro", "suc"}:
            where.append("site=%s")
            params.append(site)
        if not include_bots:
            where.append("is_bot=0")
        clause = " AND ".join(where)

        def grouped(columns: str, limit: int = 30) -> list[dict]:
            cur.execute(f"SELECT {columns}, COUNT(*) AS pv FROM pro_suc_page_views WHERE {clause} GROUP BY {columns} ORDER BY pv DESC LIMIT %s", (*params, limit))
            return cur.fetchall()

        cur.execute(f"SELECT COUNT(*) AS total, SUM(site='pro') AS pro, SUM(site='suc') AS suc, SUM(origin='legacy_db') AS estimated FROM pro_suc_page_views WHERE {clause}", tuple(params))
        totals = cur.fetchone() or {}
        cur.execute(f"SELECT DATE(viewed_at) AS day, site, COUNT(*) AS pv FROM pro_suc_page_views WHERE {clause} GROUP BY DATE(viewed_at),site ORDER BY day", tuple(params))
        daily = [{"day": row["day"].isoformat(), "site": row["site"], "pv": row["pv"]} for row in cur.fetchall()]
        return {
            "total": int(totals.get("total") or 0), "pro": int(totals.get("pro") or 0),
            "suc": int(totals.get("suc") or 0), "estimated": int(totals.get("estimated") or 0),
            "daily": daily,
            "sources": grouped("source_kind,source_label", 20),
            "referrers": grouped("referer_host", 20),
            "months": grouped("calendar_month", 30),
            "devices": grouped("device", 20), "oses": grouped("os_name", 20),
            "browsers": grouped("browser", 20), "ips": grouped("ip", 20),
            "origins": grouped("origin", 10),
        }
    finally:
        db.close()
