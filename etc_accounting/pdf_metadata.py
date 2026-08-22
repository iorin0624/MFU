from __future__ import annotations

import os
import re
import subprocess
import unicodedata


_REGISTRATION_NUMBER_RE = re.compile(r"(?<![A-Z0-9])T\d{13}(?!\d)")
_PHONE_RE = re.compile(r"(?:\d[\d\-‐‑‒–—―ー ]{7,}\d)")


def parse_invoice_registration_number(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "").upper()
    numbers = sorted(set(_REGISTRATION_NUMBER_RE.findall(normalized)))
    if not numbers:
        raise RuntimeError("ETC利用証明書PDFにインボイス登録番号が記載されていません。")
    if len(numbers) != 1:
        raise RuntimeError("ETC利用証明書PDFに複数のインボイス登録番号が含まれています。")
    return numbers[0]


def parse_invoice_issuer_name(text: str, registration_number: str) -> str:
    lines = [
        re.sub(r"\s+", " ", unicodedata.normalize("NFKC", line or "")).strip()
        for line in (text or "").splitlines()
    ]
    lines = [line for line in lines if line]
    target_index = next((i for i, line in enumerate(lines) if registration_number in line.upper()), -1)
    if target_index < 0:
        return ""
    for line in reversed(lines[max(0, target_index - 5) : target_index]):
        if _PHONE_RE.fullmatch(line):
            continue
        if "利用証明書" in line.replace(" ", ""):
            continue
        if "登録番号" in line:
            continue
        return line[:255]
    return ""


def extract_pdf_text(path: str) -> str:
    if not path or not os.path.isfile(path):
        raise RuntimeError("ETC利用証明書PDFが見つかりません。再取得してください。")
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", path, "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("PDFから登録番号を読み取る機能がありません。") from exc
    if result.returncode != 0:
        error = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"PDFから登録番号を読み取れませんでした。{error[:300]}")
    return result.stdout.decode("utf-8", "replace")


def extract_pdf_metadata(path: str) -> dict:
    text = extract_pdf_text(path)
    registration_number = parse_invoice_registration_number(text)
    return {
        "registration_number": registration_number,
        "issuer_name": parse_invoice_issuer_name(text, registration_number),
    }
