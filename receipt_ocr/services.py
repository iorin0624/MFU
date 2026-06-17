from __future__ import annotations

import base64
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import requests
from werkzeug.utils import secure_filename

from app.freee_api import services as freee_services
from app.utils.db import get_db

JST = timezone(timedelta(hours=9))
RECEIPT_OCR_ROOT = Path(os.environ.get("MFU_RECEIPT_OCR_ROOT", "/mnt/mfu/receipt_ocr"))
OPENAI_MODEL = os.environ.get("OPENAI_RECEIPT_OCR_MODEL", "gpt-4.1-mini")
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


def now_jst() -> datetime:
    return datetime.now(JST).replace(tzinfo=None)


def ensure_receipt_ocr_schema(db=None) -> None:
    close_db = False
    if db is None:
        db = get_db()
        close_db = True
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS receipt_ocr_records (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            owner_user_id VARCHAR(191) NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'draft',
            store_name VARCHAR(191) NULL,
            invoice_registration_number VARCHAR(32) NULL,
            receipt_date DATE NULL,
            total_amount_yen INT NULL,
            tax10_amount_yen INT NULL,
            tax8_amount_yen INT NULL,
            account_item_id BIGINT NULL,
            tax_code_10 INT NULL,
            tax_code_8 INT NULL,
            tax_code_nontax INT NULL,
            walletable_type VARCHAR(32) NULL,
            walletable_id BIGINT NULL,
            freee_partner_id BIGINT NULL,
            freee_partner_code VARCHAR(191) NULL,
            freee_memo_tags TEXT NULL,
            freee_deal_id BIGINT NULL,
            freee_receipt_id BIGINT NULL,
            freee_api_registered_at DATETIME NULL,
            freee_api_modified_at DATETIME NULL,
            freee_api_synced_at DATETIME NULL,
            freee_api_status VARCHAR(32) NULL,
            freee_api_error TEXT NULL,
            original_filename VARCHAR(255) NULL,
            original_image_path TEXT NULL,
            processed_image_path TEXT NULL,
            ocr_text MEDIUMTEXT NULL,
            ocr_json MEDIUMTEXT NULL,
            memo TEXT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            INDEX ix_receipt_ocr_owner_created (owner_user_id, created_at),
            INDEX ix_receipt_ocr_store (store_name),
            INDEX ix_receipt_ocr_invoice_no (invoice_registration_number)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS receipt_ocr_rules (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            match_key VARCHAR(255) NOT NULL,
            match_type VARCHAR(32) NOT NULL,
            store_name VARCHAR(191) NULL,
            invoice_registration_number VARCHAR(32) NULL,
            account_item_id BIGINT NULL,
            tax_code_10 INT NULL,
            tax_code_8 INT NULL,
            tax_code_nontax INT NULL,
            walletable_type VARCHAR(32) NULL,
            walletable_id BIGINT NULL,
            freee_partner_id BIGINT NULL,
            freee_partner_code VARCHAR(191) NULL,
            management_tags TEXT NULL,
            freee_memo_tags TEXT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            UNIQUE KEY ux_receipt_ocr_rules_key (match_type, match_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS receipt_ocr_tags (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(80) NOT NULL UNIQUE,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS receipt_ocr_record_tags (
            record_id BIGINT NOT NULL,
            tag_id BIGINT NOT NULL,
            PRIMARY KEY (record_id, tag_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    if not _column_exists(cur, "receipt_ocr_records", "freee_memo_tags"):
        cur.execute("ALTER TABLE receipt_ocr_records ADD COLUMN freee_memo_tags TEXT NULL AFTER freee_partner_code")
    db.commit()
    if close_db:
        db.close()


def _column_exists(cur, table_name: str, column_name: str) -> bool:
    cur.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND COLUMN_NAME = %s
        """,
        (table_name, column_name),
    )
    row = cur.fetchone()
    return bool((row[0] if not isinstance(row, dict) else next(iter(row.values()))) or 0)


def allowed_image(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in ALLOWED_EXTENSIONS


def record_dir(record_uuid: str) -> Path:
    path = RECEIPT_OCR_ROOT / record_uuid
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_upload(file_storage) -> tuple[str, str]:
    filename = secure_filename(file_storage.filename or "receipt.jpg") or "receipt.jpg"
    if not allowed_image(filename):
        raise ValueError("対応していない画像形式です。jpg/png/webp/heicを指定してください。")
    folder = record_dir(uuid4().hex)
    original_path = folder / f"original{Path(filename).suffix.lower() or '.jpg'}"
    file_storage.save(original_path)
    processed_path = folder / "processed.jpg"
    process_receipt_image(str(original_path), str(processed_path))
    return str(original_path), str(processed_path)


def process_receipt_image(original_path: str, processed_path: str) -> None:
    try:
        import cv2
        import numpy as np

        image = cv2.imread(original_path)
        if image is None:
            raise ValueError("image decode failed")
        ratio = image.shape[0] / 800.0
        resized = cv2.resize(image, (int(image.shape[1] / ratio), 800))
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        edged = cv2.Canny(gray, 50, 150)
        contours, _ = cv2.findContours(edged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)[:5]
        target = None
        for contour in contours:
            peri = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
            if len(approx) == 4:
                target = approx.reshape(4, 2) * ratio
                break
        if target is None:
            raise ValueError("receipt contour not found")
        rect = _order_points(target.astype("float32"))
        width_a = np.linalg.norm(rect[2] - rect[3])
        width_b = np.linalg.norm(rect[1] - rect[0])
        height_a = np.linalg.norm(rect[1] - rect[2])
        height_b = np.linalg.norm(rect[0] - rect[3])
        max_width = int(max(width_a, width_b))
        max_height = int(max(height_a, height_b))
        dst = np.array([[0, 0], [max_width - 1, 0], [max_width - 1, max_height - 1], [0, max_height - 1]], dtype="float32")
        matrix = cv2.getPerspectiveTransform(rect, dst)
        warped = cv2.warpPerspective(image, matrix, (max_width, max_height))
        cv2.imwrite(processed_path, warped)
        return
    except Exception:
        pass
    try:
        from PIL import Image

        with Image.open(original_path) as im:
            im = im.convert("RGB")
            im.thumbnail((1600, 2400))
            im.save(processed_path, "JPEG", quality=92)
    except Exception:
        shutil.copyfile(original_path, processed_path)


def _make_ocr_image_variants(image_path: str) -> list[str]:
    variants = [image_path]
    try:
        from PIL import Image, ImageOps, ImageFilter

        base = Path(image_path)
        with Image.open(image_path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((2200, 3200))
            normalized_path = str(base.with_name("ocr_normalized.jpg"))
            im.save(normalized_path, "JPEG", quality=94)
            variants.append(normalized_path)

            for angle, suffix in ((90, "rot90"), (-90, "rot270")):
                rotated = im.rotate(angle, expand=True)
                rotated_path = str(base.with_name(f"ocr_{suffix}.jpg"))
                rotated.save(rotated_path, "JPEG", quality=94)
                variants.append(rotated_path)

            enhanced = ImageOps.autocontrast(im.convert("L")).filter(ImageFilter.SHARPEN).convert("RGB")
            enhanced_path = str(base.with_name("ocr_enhanced.jpg"))
            enhanced.save(enhanced_path, "JPEG", quality=94)
            variants.append(enhanced_path)
    except Exception:
        return variants

    unique = []
    seen = set()
    for path in variants:
        if path not in seen and os.path.exists(path):
            unique.append(path)
            seen.add(path)
    return unique[:5]


def _order_points(points):
    import numpy as np

    rect = np.zeros((4, 2), dtype="float32")
    s = points.sum(axis=1)
    rect[0] = points[np.argmin(s)]
    rect[2] = points[np.argmax(s)]
    diff = np.diff(points, axis=1)
    rect[1] = points[np.argmin(diff)]
    rect[3] = points[np.argmax(diff)]
    return rect


def _image_data_url(path: str) -> str:
    ext = Path(path).suffix.lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    with open(path, "rb") as f:
        return f"data:{mime};base64,{base64.b64encode(f.read()).decode('ascii')}"


def analyze_receipt_with_openai(image_path: str) -> dict:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が未設定です。")
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "date": {"type": ["string", "null"]},
            "store_name": {"type": ["string", "null"]},
            "invoice_registration_number": {"type": ["string", "null"]},
            "total_amount_yen": {"type": ["integer", "null"]},
            "tax10_amount_yen": {"type": ["integer", "null"]},
            "tax8_amount_yen": {"type": ["integer", "null"]},
            "raw_text": {"type": "string"},
        },
        "required": ["date", "store_name", "invoice_registration_number", "total_amount_yen", "tax10_amount_yen", "tax8_amount_yen", "raw_text"],
    }
    prompt = (
        "日本のレシート画像から情報を抽出してください。"
        "同じレシートの補正候補を複数枚渡します。横向き・逆向き・高コントラスト版が混ざるため、"
        "最も読みやすい候補を選び、他候補も照合して正確に読んでください。"
        "日付、店名、Tから始まるインボイス登録番号、合計金額、10%対象小計、8%対象小計、OCR全文を返してください。"
        "ガソリンスタンドのレシートでは店名にブランド名だけでなく運営会社名/店舗名があればそれを優先してください。"
        "レジ番号、問い合わせ番号、ポイント番号、カード番号、電話番号を合計金額やインボイス番号と混同しないでください。"
        "税込/税抜表記が混在する場合はレシートに書かれている対象小計として最も妥当な金額を整数円で返してください。"
        "読めない項目は推測せずnullにしてください。OCR全文は読み取れた主要行を上から順に、改行付きで返してください。"
    )
    content = [{"type": "input_text", "text": prompt}]
    for variant_path in _make_ocr_image_variants(image_path):
        content.append({"type": "input_image", "image_url": _image_data_url(variant_path), "detail": "high"})
    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "receipt_ocr",
                "schema": schema,
                "strict": True,
            }
        },
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    if not (200 <= response.status_code < 300):
        raise RuntimeError(f"OpenAI OCR error: HTTP {response.status_code} {response.text[:1000]}")
    data = response.json()
    text = data.get("output_text") or ""
    if not text:
        for item in data.get("output") or []:
            for content in item.get("content") or []:
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    text += content["text"]
    if not text:
        raise RuntimeError("OpenAI OCRのJSON応答を取得できませんでした。")
    parsed = json.loads(text)
    parsed["invoice_registration_number"] = normalize_invoice_no(parsed.get("invoice_registration_number"))
    return parsed


def normalize_invoice_no(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"T[0-9０-９]{13}", str(value).replace("-", "").replace(" ", ""))
    if not match:
        return None
    return "T" + "".join(str.maketrans("０１２３４５６７８９", "0123456789").get(ch, ch) for ch in match.group(0)[1:])


def parse_date_or_none(value: str | None):
    if not value:
        return None
    text = str(value).strip().translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3))).date()
        except ValueError:
            return None
    normalized = text.replace("/", "-").replace(".", "-")
    try:
        return datetime.strptime(normalized[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def infer_receipt_date(record: dict):
    current = record.get("receipt_date")
    if current:
        return current
    try:
        ocr = json.loads(record.get("ocr_json") or "{}")
        parsed = parse_date_or_none(ocr.get("date"))
        if parsed:
            return parsed
    except Exception:
        pass
    return parse_date_or_none(record.get("ocr_text"))


def int_or_none(value):
    try:
        if value is None or value == "":
            return None
        return int(float(str(value).replace(",", "")))
    except Exception:
        return None


def split_tags(value: str | None) -> list[str]:
    if not value:
        return []
    tags = []
    for part in re.split(r"[,、\s]+", value):
        tag = part.strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def tags_to_text(tags: list[str]) -> str:
    return " ".join(tags)


def get_tags_for_record(cur, record_id: int) -> list[str]:
    cur.execute(
        """
        SELECT t.name
        FROM receipt_ocr_tags t
        JOIN receipt_ocr_record_tags rt ON rt.tag_id = t.id
        WHERE rt.record_id = %s
        ORDER BY t.name
        """,
        (record_id,),
    )
    return [row[0] if not isinstance(row, dict) else row["name"] for row in cur.fetchall()]


def set_record_tags(cur, record_id: int, tag_names: list[str]) -> None:
    now = now_jst()
    cur.execute("DELETE FROM receipt_ocr_record_tags WHERE record_id = %s", (record_id,))
    for name in tag_names:
        cur.execute(
            """
            INSERT INTO receipt_ocr_tags (name, created_at, updated_at)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE updated_at = VALUES(updated_at)
            """,
            (name, now, now),
        )
        cur.execute("SELECT id FROM receipt_ocr_tags WHERE name = %s", (name,))
        row = cur.fetchone()
        tag_id = row[0] if not isinstance(row, dict) else row["id"]
        cur.execute(
            "INSERT IGNORE INTO receipt_ocr_record_tags (record_id, tag_id) VALUES (%s, %s)",
            (record_id, tag_id),
        )


def find_rule(cur, invoice_no: str | None, store_name: str | None) -> dict | None:
    if invoice_no:
        cur.execute("SELECT * FROM receipt_ocr_rules WHERE match_type='invoice_no' AND match_key=%s", (invoice_no,))
        row = cur.fetchone()
        if row:
            return _dict_row(cur, row)
    if store_name:
        cur.execute("SELECT * FROM receipt_ocr_rules WHERE match_type='store_name' AND match_key=%s", (store_name,))
        row = cur.fetchone()
        if row:
            return _dict_row(cur, row)
    return None


def _dict_row(cur, row):
    if row is None or isinstance(row, dict):
        return row
    return dict(zip([d[0] for d in cur.description], row))


def upsert_rule_from_record(cur, record: dict, management_tags: list[str], freee_memo_tags: str | None) -> None:
    match_type = "invoice_no" if record.get("invoice_registration_number") else "store_name"
    match_key = record.get("invoice_registration_number") or record.get("store_name")
    if not match_key:
        return
    now = now_jst()
    cur.execute(
        """
        INSERT INTO receipt_ocr_rules (
            match_key, match_type, store_name, invoice_registration_number,
            account_item_id, tax_code_10, tax_code_8, tax_code_nontax,
            walletable_type, walletable_id, freee_partner_id, freee_partner_code,
            management_tags, freee_memo_tags, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            store_name=VALUES(store_name),
            invoice_registration_number=VALUES(invoice_registration_number),
            account_item_id=VALUES(account_item_id),
            tax_code_10=VALUES(tax_code_10),
            tax_code_8=VALUES(tax_code_8),
            tax_code_nontax=VALUES(tax_code_nontax),
            walletable_type=VALUES(walletable_type),
            walletable_id=VALUES(walletable_id),
            freee_partner_id=VALUES(freee_partner_id),
            freee_partner_code=VALUES(freee_partner_code),
            management_tags=VALUES(management_tags),
            freee_memo_tags=VALUES(freee_memo_tags),
            updated_at=VALUES(updated_at)
        """,
        (
            match_key, match_type, record.get("store_name"), record.get("invoice_registration_number"),
            record.get("account_item_id"), record.get("tax_code_10"), record.get("tax_code_8"), record.get("tax_code_nontax"),
            record.get("walletable_type"), record.get("walletable_id"), record.get("freee_partner_id"), record.get("freee_partner_code"),
            tags_to_text(management_tags), freee_memo_tags, now, now,
        ),
    )


def apply_rule_to_record(cur, record_id: int, rule: dict | None) -> None:
    if not rule:
        return
    now = now_jst()
    cur.execute(
        """
        UPDATE receipt_ocr_records
        SET account_item_id = COALESCE(account_item_id, %s),
            tax_code_10 = COALESCE(tax_code_10, %s),
            tax_code_8 = COALESCE(tax_code_8, %s),
            tax_code_nontax = COALESCE(tax_code_nontax, %s),
            walletable_type = COALESCE(walletable_type, %s),
            walletable_id = COALESCE(walletable_id, %s),
            freee_partner_id = COALESCE(freee_partner_id, %s),
            freee_partner_code = COALESCE(freee_partner_code, %s),
            updated_at = %s
        WHERE id = %s
        """,
        (
            rule.get("account_item_id"), rule.get("tax_code_10"), rule.get("tax_code_8"), rule.get("tax_code_nontax"),
            rule.get("walletable_type"), rule.get("walletable_id"), rule.get("freee_partner_id"), rule.get("freee_partner_code"),
            now, record_id,
        ),
    )
    set_record_tags(cur, record_id, split_tags(rule.get("management_tags")))


def receipt_needs_freee_resync(record: dict) -> bool:
    if not record.get("freee_deal_id"):
        return False
    synced_at = record.get("freee_api_synced_at")
    updated_at = record.get("updated_at")
    if not synced_at:
        return True
    return bool(updated_at and updated_at > synced_at)


def resolve_freee_partner(record: dict, company_id: int) -> int | None:
    if record.get("freee_partner_id"):
        return int(record["freee_partner_id"])
    store_name = (record.get("store_name") or "").strip()
    if not store_name:
        return None
    data = freee_services.freee_api_request("GET", "/api/1/partners", params={"company_id": company_id, "keyword": store_name})
    partners = freee_services.freee_list_from_response(data, "partners")
    partner = next((p for p in partners if str(p.get("name") or "") == store_name), None) or (partners[0] if partners else None)
    if not partner:
        created = freee_services.freee_api_request("POST", "/api/1/partners", json_body={"company_id": company_id, "name": store_name})
        partner = created.get("partner") if isinstance(created, dict) else created
    if partner and partner.get("id"):
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "UPDATE receipt_ocr_records SET freee_partner_id=%s, freee_partner_code=%s WHERE id=%s",
            (partner.get("id"), partner.get("code"), record["id"]),
        )
        db.commit()
        db.close()
        return int(partner["id"])
    return None


def build_freee_deal_payload(record: dict, company_id: int) -> dict:
    details = []
    tax10 = int_or_none(record.get("tax10_amount_yen")) or 0
    tax8 = int_or_none(record.get("tax8_amount_yen")) or 0
    total = int_or_none(record.get("total_amount_yen")) or 0
    base_description = record.get("store_name") or "レシート"
    if tax10 > 0:
        details.append({"account_item_id": int(record["account_item_id"]), "tax_code": int(record["tax_code_10"]), "amount": tax10, "description": f"{base_description} 10%"})
    if tax8 > 0:
        details.append({"account_item_id": int(record["account_item_id"]), "tax_code": int(record["tax_code_8"]), "amount": tax8, "description": f"{base_description} 8%"})
    if not details and total > 0:
        details.append({"account_item_id": int(record["account_item_id"]), "tax_code": int(record["tax_code_10"]), "amount": total, "description": base_description})
    if not details:
        raise RuntimeError("freeeに登録できる金額がありません。")
    issue_date = record["receipt_date"].isoformat() if hasattr(record["receipt_date"], "isoformat") else str(record["receipt_date"])
    payload = {
        "company_id": company_id,
        "issue_date": issue_date,
        "due_date": issue_date,
        "type": "expense",
        "ref_number": f"receipt-ocr-{record['id']}",
        "details": details,
        "payments": [{
            "date": issue_date,
            "from_walletable_type": record["walletable_type"],
            "from_walletable_id": int(record["walletable_id"]),
            "amount": sum(int(d["amount"]) for d in details),
        }],
    }
    partner_id = resolve_freee_partner(record, company_id)
    if partner_id:
        payload["partner_id"] = partner_id
    tag_ids = [int(value) for value in re.findall(r"\d+", record.get("freee_memo_tags") or "")]
    if tag_ids:
        payload["tag_ids"] = tag_ids
    return payload


def sync_freee_deal_payment(deal_id: int, payload: dict) -> None:
    payment = dict((payload.get("payments") or [])[0])
    payment["company_id"] = payload["company_id"]
    data = freee_services.freee_api_request(
        "GET",
        f"/api/1/deals/{deal_id}",
        params={"company_id": payload["company_id"]},
    )
    deal = data.get("deal") if isinstance(data, dict) else {}
    payments = (deal or {}).get("payments") or []
    if payments and payments[0].get("id"):
        freee_services.freee_api_request(
            "PUT",
            f"/api/1/deals/{deal_id}/payments/{payments[0]['id']}",
            json_body=payment,
        )
        return
    freee_services.freee_api_request("POST", f"/api/1/deals/{deal_id}/payments", json_body=payment)


def upload_receipt_file_to_freee(record: dict, company_id: int) -> int | None:
    path = record.get("processed_image_path") or record.get("original_image_path")
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            data = freee_services.freee_api_multipart_request(
                "POST",
                "/api/1/receipts",
                data={"company_id": str(company_id), "description": record.get("store_name") or "receipt"},
                files={"file": (os.path.basename(path), f, "image/jpeg")},
            )
        receipt = data.get("receipt") if isinstance(data, dict) else None
        return int((receipt or {}).get("id") or data.get("id")) if ((receipt or {}).get("id") or data.get("id")) else None
    except Exception:
        return None


def mark_freee_error(record_id: int, message: str) -> None:
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "UPDATE receipt_ocr_records SET freee_api_status='error', freee_api_error=%s, updated_at=%s WHERE id=%s",
        (freee_services.sanitize_freee_error(message), now_jst(), record_id),
    )
    db.commit()
    db.close()


def sync_record_to_freee(record: dict) -> dict:
    common = freee_services.get_freee_common_settings()
    company_id = int((common or {}).get("company_id") or 0)
    if not company_id:
        raise RuntimeError("freee共通設定の事業所が未設定です。")
    required = ("account_item_id", "tax_code_10", "tax_code_8", "walletable_type", "walletable_id", "receipt_date")
    missing = [key for key in required if not record.get(key)]
    if missing:
        raise RuntimeError("freee登録に必要な項目が未設定です: " + ", ".join(missing))
    if record.get("freee_deal_id") and not receipt_needs_freee_resync(record):
        return {"status": "skipped_already_synced", "freee_deal_id": int(record["freee_deal_id"])}
    payload = build_freee_deal_payload(record, company_id)
    deal_payload = dict(payload)
    deal_payload.pop("payments", None)
    try:
        status = "synced"
        if record.get("freee_deal_id"):
            deal_id = int(record["freee_deal_id"])
            freee_services.freee_api_request("PUT", f"/api/1/deals/{deal_id}", json_body=deal_payload)
            sync_freee_deal_payment(deal_id, payload)
            status = "updated"
        else:
            data = freee_services.freee_api_request("POST", "/api/1/deals", json_body=payload)
            deal = data.get("deal") if isinstance(data, dict) else None
            deal_id = int((deal or {}).get("id") or data.get("id"))
        file_id = upload_receipt_file_to_freee(record, company_id)
        db = get_db()
        cur = db.cursor()
        now = now_jst()
        if status == "updated":
            cur.execute(
                """
                UPDATE receipt_ocr_records
                SET freee_deal_id=%s, freee_receipt_id=COALESCE(%s, freee_receipt_id),
                    freee_api_synced_at=%s, freee_api_modified_at=%s,
                    freee_api_status='synced', freee_api_error=NULL, updated_at=updated_at
                WHERE id=%s
                """,
                (deal_id, file_id, now, now, record["id"]),
            )
        else:
            cur.execute(
                """
                UPDATE receipt_ocr_records
                SET freee_deal_id=%s, freee_receipt_id=COALESCE(%s, freee_receipt_id),
                    freee_api_synced_at=%s, freee_api_registered_at=%s, freee_api_modified_at=NULL,
                    freee_api_status='synced', freee_api_error=NULL, updated_at=updated_at
                WHERE id=%s
                """,
                (deal_id, file_id, now, now, record["id"]),
            )
        db.commit()
        db.close()
        return {"status": status, "freee_deal_id": deal_id}
    except Exception as exc:
        mark_freee_error(int(record["id"]), str(exc))
        raise
