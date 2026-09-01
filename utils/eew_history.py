"""Read-only access helpers for the Raspberry Pi EEW history database."""

from __future__ import annotations

import configparser
from datetime import date, datetime, timedelta
import json
from pathlib import Path

import mysql.connector


CONFIG_PATH = Path("/etc/mfu/eew-history-reader.conf")
SCALE_LABELS = {
    -1: "不明", 0: "0", 10: "1", 20: "2", 30: "3", 40: "4",
    45: "5弱", 50: "5強", 55: "6弱", 60: "6強", 70: "7",
}


def _config(path=CONFIG_PATH):
    parser = configparser.ConfigParser()
    if not parser.read(path, encoding="utf-8") or not parser.has_section("mysql"):
        raise RuntimeError("EEW履歴DBの設定ファイルを読み込めません。")
    section = parser["mysql"]
    return {
        "host": section.get("host", "192.168.103.17"),
        "port": section.getint("port", 3306),
        "database": section.get("database", "eew_history"),
        "user": section["user"],
        "password": section["password"],
        "connection_timeout": section.getint("connect_timeout", 5),
        "charset": "utf8mb4",
        "use_unicode": True,
    }


def connect():
    return mysql.connector.connect(**_config())


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


def _where(filters):
    clauses = []
    params = []
    date_from = _parse_date(filters.get("date_from"))
    date_to = _parse_date(filters.get("date_to"))
    if date_from:
        clauses.append("COALESCE(jma_issue_time, p2p_time, first_pi_received_at) >= %s")
        params.append(datetime.combine(date_from, datetime.min.time()))
    if date_to:
        clauses.append("COALESCE(jma_issue_time, p2p_time, first_pi_received_at) < %s")
        params.append(datetime.combine(date_to + timedelta(days=1), datetime.min.time()))

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append("(event_id LIKE %s OR hypocenter_name LIKE %s)")
        like = f"%{keyword}%"
        params.extend((like, like))

    source = filters.get("source") or ""
    if source == "ws":
        clauses.append("pi_ws_received_at IS NOT NULL")
    elif source == "history":
        clauses.append("pi_history_received_at IS NOT NULL")
    elif source == "both":
        clauses.append("pi_ws_received_at IS NOT NULL AND pi_history_received_at IS NOT NULL")

    cancelled = filters.get("cancelled")
    if cancelled in ("0", "1"):
        clauses.append("cancelled = %s")
        params.append(int(cancelled))
    discord = filters.get("discord")
    if discord in ("0", "1"):
        clauses.append("discord_notified = %s")
        params.append(int(discord))
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _format_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if isinstance(value, datetime) else "-"


def _seconds_between(start, end):
    if not isinstance(start, datetime) or not isinstance(end, datetime):
        return None
    return round((end - start).total_seconds(), 3)


def scale_label(value):
    try:
        return SCALE_LABELS.get(int(value), str(value))
    except (TypeError, ValueError):
        return "不明"


def enrich_report(row, include_raw=False):
    row = dict(row)
    datetime_fields = (
        "jma_issue_time", "p2p_time", "pi_ws_received_at", "pi_history_received_at",
        "first_pi_received_at", "last_pi_received_at", "origin_time", "arrival_time",
        "created_at", "updated_at",
    )
    row["formatted"] = {key: _format_dt(row.get(key)) for key in datetime_fields}
    row["delays"] = {
        "jma_to_p2p": _seconds_between(row.get("jma_issue_time"), row.get("p2p_time")),
        "p2p_to_ws": _seconds_between(row.get("p2p_time"), row.get("pi_ws_received_at")),
        "jma_to_ws": _seconds_between(row.get("jma_issue_time"), row.get("pi_ws_received_at")),
    }
    row["scale_from_label"] = scale_label(row.get("scale_from"))
    row["scale_to_label"] = scale_label(row.get("scale_to"))
    row["has_ws"] = row.get("pi_ws_received_at") is not None
    row["has_history"] = row.get("pi_history_received_at") is not None
    if include_raw:
        raw = row.get("raw_json") or ""
        try:
            row["raw_json_pretty"] = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except Exception:
            row["raw_json_pretty"] = raw
    else:
        row.pop("raw_json", None)
    return row


def list_reports(filters, page=1, per_page=50):
    page = max(1, int(page or 1))
    per_page = max(10, min(200, int(per_page or 50)))
    where_sql, params = _where(filters)
    db = connect()
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT COUNT(*) AS total FROM eew_reports{where_sql}", params)
        total = int(cursor.fetchone()["total"])
        cursor.execute(
            f"""
            SELECT id, event_id, serial, code, jma_issue_time, p2p_time,
                   pi_ws_received_at, pi_history_received_at,
                   first_pi_received_at, last_pi_received_at,
                   first_source, last_source, receive_count, cancelled,
                   origin_time, arrival_time, hypocenter_name, magnitude,
                   depth_km, latitude, longitude, scale_from, scale_to,
                   discord_notified, created_at, updated_at
              FROM eew_reports
              {where_sql}
             ORDER BY COALESCE(jma_issue_time, p2p_time, first_pi_received_at) DESC,
                      event_id DESC, serial DESC
             LIMIT %s OFFSET %s
            """,
            params + [per_page, (page - 1) * per_page],
        )
        rows = [enrich_report(row) for row in cursor.fetchall()]
        cursor.execute(
            f"""
            SELECT COUNT(*) AS reports,
                   COUNT(DISTINCT event_id) AS events,
                   COALESCE(SUM(cancelled), 0) AS cancelled_count,
                   COALESCE(SUM(discord_notified = 0), 0) AS discord_not_notified,
                   AVG(CASE WHEN jma_issue_time IS NOT NULL AND pi_ws_received_at IS NOT NULL
                       THEN TIMESTAMPDIFF(MICROSECOND, jma_issue_time, pi_ws_received_at) / 1000000 END)
                       AS avg_jma_to_ws
              FROM eew_reports
              {where_sql}
            """,
            params,
        )
        summary = cursor.fetchone()
    finally:
        cursor.close()
        db.close()
    pages = max(1, (total + per_page - 1) // per_page)
    return rows, summary, {"page": page, "per_page": per_page, "total": total, "pages": pages}


def get_report(report_id):
    db = connect()
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM eew_reports WHERE id = %s", (int(report_id),))
        row = cursor.fetchone()
        return enrich_report(row, include_raw=True) if row else None
    finally:
        cursor.close()
        db.close()

