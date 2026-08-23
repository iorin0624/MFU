"""Fetch and parse Chrony monitoring data for the admin UI."""

from __future__ import annotations

from datetime import datetime, timezone
import re
import ipaddress
import threading

import requests


_schema_lock = threading.Lock()
_schema_ready = False


def normalize_client_address(value):
    address = (value or "").strip()
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        # Chrony can expose localhost or a reverse-DNS hostname.  Keep these
        # bounded and conservative even though the normal API uses -n.
        if re.fullmatch(r"[A-Za-z0-9._:-]{1,255}", address):
            return address.lower()
        raise ValueError("クライアントアドレスの形式が不正です。")


def ensure_client_label_schema(db):
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        cursor = db.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS chrony_client_labels (
                    client_address VARCHAR(255) NOT NULL PRIMARY KEY,
                    display_name VARCHAR(100) NOT NULL,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            db.commit()
            _schema_ready = True
        finally:
            cursor.close()


def load_client_labels(db):
    ensure_client_label_schema(db)
    cursor = db.cursor(dictionary=True)
    try:
        cursor.execute("SELECT client_address, display_name FROM chrony_client_labels")
        return {row["client_address"]: row["display_name"] for row in cursor.fetchall()}
    finally:
        cursor.close()


def save_client_label(db, address, display_name):
    address = normalize_client_address(address)
    name = (display_name or "").strip()
    if len(name) > 100:
        raise ValueError("表示名は100文字以内で入力してください。")
    ensure_client_label_schema(db)
    cursor = db.cursor()
    try:
        if name:
            cursor.execute(
                """
                INSERT INTO chrony_client_labels (client_address, display_name)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE display_name = VALUES(display_name)
                """,
                (address, name),
            )
        else:
            cursor.execute(
                "DELETE FROM chrony_client_labels WHERE client_address = %s",
                (address,),
            )
        db.commit()
    finally:
        cursor.close()
    return address, name


TRACKING_FLOAT_KEYS = {
    "System time": "system_time_seconds",
    "Last offset": "last_offset_seconds",
    "RMS offset": "rms_offset_seconds",
    "Frequency": "frequency_ppm",
    "Residual freq": "residual_frequency_ppm",
    "Skew": "skew_ppm",
    "Root delay": "root_delay_seconds",
    "Root dispersion": "root_dispersion_seconds",
    "Update interval": "update_interval_seconds",
}


def _command_stdout(payload, name):
    command = (payload.get("commands") or {}).get(name) or {}
    return command.get("stdout") or ""


def _first_float(value):
    match = re.search(r"[-+]?\d+(?:\.\d+)?", value or "")
    return float(match.group(0)) if match else None


def parse_tracking(text):
    data = {}
    raw_values = {}
    for raw_line in (text or "").splitlines():
        if ":" not in raw_line:
            continue
        key, value = (part.strip() for part in raw_line.split(":", 1))
        raw_values[key] = value
        if key in TRACKING_FLOAT_KEYS:
            data[TRACKING_FLOAT_KEYS[key]] = _first_float(value)
            if key == "System time" and "slow" in value.lower():
                data[TRACKING_FLOAT_KEYS[key]] = -abs(data[TRACKING_FLOAT_KEYS[key]])
        elif key == "Stratum":
            value_float = _first_float(value)
            data["stratum"] = int(value_float) if value_float is not None else None
        elif key == "Reference ID":
            data["reference_id"] = value
        elif key == "Ref time (UTC)":
            data["reference_time_utc"] = value
        elif key == "Leap status":
            data["leap_status"] = value
    data["raw_values"] = raw_values
    return data


def _table_lines(text):
    lines = (text or "").splitlines()
    separator = next((i for i, line in enumerate(lines) if set(line.strip()) == {"="}), None)
    return lines[separator + 1:] if separator is not None else []


def parse_sources(text):
    rows = []
    pattern = re.compile(
        r"^(?P<mode>.)(?P<state>.)\s+(?P<name>\S+)\s+(?P<stratum>\d+)\s+"
        r"(?P<poll>\d+)\s+(?P<reach>\d+)\s+(?P<last_rx>\S+)\s+(?P<sample>.+)$"
    )
    for line in _table_lines(text):
        match = pattern.match(line.strip())
        if not match:
            continue
        item = match.groupdict()
        for key in ("stratum", "poll"):
            item[key] = int(item[key])
        item["reach"] = item["reach"]
        rows.append(item)
    return rows


def parse_selectdata(text):
    rows = []
    pattern = re.compile(
        r"^(?P<state>\S)\s+(?P<name>\S+)\s+(?P<auth>\S)\s+"
        r"(?P<configured>\S+)\s+(?P<effective>\S+)\s+(?P<last>\S+)\s+"
        r"(?P<score>\S+)\s+(?P<interval>.+?)\s+(?P<leap>\S)$"
    )
    for line in _table_lines(text):
        match = pattern.match(line.strip())
        if match:
            rows.append(match.groupdict())
    return rows


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_client(last_seconds, interval_log2, hits, drops):
    if drops and drops > 0:
        return "warning"
    if not hits:
        return "unknown"
    if last_seconds is None:
        return "unknown"
    interval_seconds = (2 ** interval_log2) if interval_log2 is not None else None
    active_limit = max(300, (interval_seconds or 0) * 3)
    warning_limit = max(900, (interval_seconds or 0) * 10)
    if last_seconds <= active_limit:
        return "active"
    if last_seconds <= warning_limit:
        return "warning"
    return "stale"


def parse_clients(text, numeric_text=None):
    display_lines = _table_lines(text)
    numeric_lines = _table_lines(numeric_text or text)
    rows = []
    for index, line in enumerate(numeric_lines):
        parts = line.split()
        if len(parts) < 9:
            continue
        display_parts = display_lines[index].split() if index < len(display_lines) else parts
        address = parts[0]
        hostname = display_parts[0] if display_parts else address
        values = parts[1:]
        item = {
            "address": address,
            "hostname": hostname,
            "ntp_hits": _int_or_none(values[0]) or 0,
            "ntp_drops": _int_or_none(values[1]) or 0,
            "ntp_interval_log2": _int_or_none(values[2]),
            "ntp_interval_limit": values[3],
            "ntp_last_seconds": _int_or_none(values[4]),
            "command_hits": _int_or_none(values[5]) or 0,
            "command_drops": _int_or_none(values[6]) or 0,
            "command_interval_log2": _int_or_none(values[7]),
            "command_last_seconds": _int_or_none(values[8]),
        }
        item["status"] = classify_client(
            item["ntp_last_seconds"], item["ntp_interval_log2"],
            item["ntp_hits"], item["ntp_drops"],
        )
        rows.append(item)
    return rows


def parse_serverstats(text):
    values = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        values[key] = _int_or_none(value)
    return values


def build_status(payload):
    tracking = parse_tracking(_command_stdout(payload, "tracking"))
    clients = parse_clients(
        _command_stdout(payload, "clients"),
        _command_stdout(payload, "clients_numeric"),
    )
    commands = payload.get("commands") or {}
    failed_commands = [name for name, result in commands.items() if not result.get("ok")]
    level = "normal"
    messages = []
    if not payload.get("ok") or failed_commands or not tracking:
        level = "error"
        messages.append("Chrony情報の一部を取得できませんでした。")
    elif tracking.get("leap_status") != "Normal" or not tracking.get("reference_id"):
        level = "error"
        messages.append("時刻同期が正常状態ではありません。")
    else:
        offset = abs(tracking.get("system_time_seconds") or 0)
        if offset > 0.01 or any(c["status"] in ("warning", "stale") for c in clients):
            level = "warning"
            messages.append("時刻差またはクライアント応答に注意が必要です。")
        else:
            messages.append("Chronyは正常に同期しています。")
    return {
        "ok": level != "error",
        "level": level,
        "messages": messages,
        "collected_at": payload.get("collected_at"),
        "host": payload.get("host"),
        "tracking": tracking,
        "sources": parse_sources(_command_stdout(payload, "sources")),
        "selectdata": parse_selectdata(_command_stdout(payload, "selectdata")),
        "clients": clients,
        "serverstats": parse_serverstats(_command_stdout(payload, "serverstats")),
        "failed_commands": failed_commands,
        "raw": {name: (value or {}).get("stdout", "") for name, value in commands.items()},
    }


def fetch_chrony_status(url, token="", timeout=5):
    headers = {"X-Node-Token": token} if token else {}
    response = requests.get(url, headers=headers, timeout=timeout)
    payload = response.json()
    # A partial payload from a 503 still contains useful command output.
    if response.status_code >= 500 and not payload.get("commands"):
        response.raise_for_status()
    return build_status(payload)


def fetch_chrony_time_sample(url, token="", timeout=4):
    headers = {"X-Node-Token": token} if token else {}
    response = requests.get(url, headers=headers, timeout=timeout)
    payload = response.json()
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "Chrony time sample is unavailable")
    required = ("sample_time_unix_ms", "ntp_time_unix_ms", "reference_id")
    if any(payload.get(key) is None for key in required):
        raise RuntimeError("Chrony time sample is incomplete")
    return payload
