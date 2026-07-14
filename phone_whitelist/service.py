from __future__ import annotations

import base64
import csv
import io
import re
import unicodedata


MAX_CSV_BYTES = 1_000_000
MAX_CSV_ROWS = 10_000
MAX_NAME_LENGTH = 100
MAX_NOTE_LENGTH = 500
MAX_SIP_CALLER_NAME_LENGTH = 32


class WhitelistValidationError(ValueError):
    pass


def normalize_phone_number(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    compact = re.sub(r"[\s()\-]", "", text)
    if compact.startswith("+"):
        compact = compact[1:]
    if re.fullmatch(r"81\d{9,10}", compact):
        compact = "0" + compact[2:]
    if not re.fullmatch(r"0\d{9,10}", compact):
        raise WhitelistValidationError(f"電話番号の形式が不正です: {text or '(空欄)'}")
    return compact


def validate_entry(phone_number: object, name: object, note: object) -> dict[str, str]:
    phone = normalize_phone_number(phone_number)
    clean_name = str(name or "").strip()
    clean_note = str(note or "").strip()
    if len(clean_name) > MAX_NAME_LENGTH:
        raise WhitelistValidationError(f"名称は{MAX_NAME_LENGTH}文字以内で入力してください")
    if len(clean_note) > MAX_NOTE_LENGTH:
        raise WhitelistValidationError(f"備考は{MAX_NOTE_LENGTH}文字以内で入力してください")
    return {"phone_number": phone, "name": clean_name, "note": clean_note}


def sanitize_sip_caller_name(value: object) -> str:
    text = str(value or "").strip()
    safe = "".join(char for char in text if not unicodedata.category(char).startswith("C"))
    return safe[:MAX_SIP_CALLER_NAME_LENGTH]


def decode_csv_bytes(data: bytes) -> str:
    if not data:
        raise WhitelistValidationError("CSVファイルが空です")
    if len(data) > MAX_CSV_BYTES:
        raise WhitelistValidationError("CSVファイルは1MB以内にしてください")
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise WhitelistValidationError("CSVはUTF-8またはShift_JISで保存してください")


def parse_csv_bytes(data: bytes) -> list[dict[str, str]]:
    text = decode_csv_bytes(data)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise WhitelistValidationError("CSVヘッダーがありません")

    aliases = {
        "phone_number": {"phone_number", "phone", "tel", "電話番号"},
        "name": {"name", "名称", "名前"},
        "note": {"note", "備考", "メモ"},
    }
    normalized_headers = {
        unicodedata.normalize("NFKC", str(header or "")).strip().lower(): header
        for header in reader.fieldnames
    }
    columns: dict[str, str | None] = {}
    for target, candidates in aliases.items():
        columns[target] = next(
            (normalized_headers[candidate] for candidate in candidates if candidate in normalized_headers),
            None,
        )
    if not columns["phone_number"]:
        raise WhitelistValidationError("phone_number（電話番号）列が必要です")

    entries: list[dict[str, str]] = []
    seen: dict[str, int] = {}
    for line_no, row in enumerate(reader, start=2):
        if line_no > MAX_CSV_ROWS + 1:
            raise WhitelistValidationError(f"CSVは{MAX_CSV_ROWS}件以内にしてください")
        if not any(str(value or "").strip() for value in row.values()):
            continue
        try:
            entry = validate_entry(
                row.get(columns["phone_number"] or "", ""),
                row.get(columns["name"] or "", "") if columns["name"] else "",
                row.get(columns["note"] or "", "") if columns["note"] else "",
            )
        except WhitelistValidationError as exc:
            raise WhitelistValidationError(f"CSV {line_no}行目: {exc}") from exc
        previous_line = seen.get(entry["phone_number"])
        if previous_line:
            raise WhitelistValidationError(
                f"CSV {line_no}行目: {entry['phone_number']} は{previous_line}行目と重複しています"
            )
        seen[entry["phone_number"]] = line_no
        entries.append(entry)

    if not entries:
        raise WhitelistValidationError("CSVに登録対象の電話番号がありません")
    return entries


def build_pbx_payload(
    entries: list[dict[str, object] | str],
    *,
    whitelist_disabled_until: int = 0,
    anonymous_allowed_until: int = 0,
) -> str:
    normalized: dict[str, str] = {}
    for entry in entries:
        if isinstance(entry, str):
            phone = normalize_phone_number(entry)
            name = ""
        else:
            phone = normalize_phone_number(entry.get("phone_number"))
            name = sanitize_sip_caller_name(entry.get("name"))
        normalized[phone] = base64.b64encode(name.encode("utf-8")).decode("ascii")
    for value in (whitelist_disabled_until, anonymous_allowed_until):
        if not isinstance(value, int) or value < 0:
            raise WhitelistValidationError("切替期限が不正です")
    lines = [
        "# Managed by MFU.2 phone whitelist",
        f"# MFU_WHITELIST_DISABLED_UNTIL={whitelist_disabled_until}",
        f"# MFU_ANONYMOUS_ALLOWED_UNTIL={anonymous_allowed_until}",
    ]
    lines.extend(f"{phone}|{normalized[phone]}" for phone in sorted(normalized))
    return "\n".join(lines) + "\n"
