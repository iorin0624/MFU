from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from app.freee_api import services as freee_services


def _normalized(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _similarity(left: str | None, right: str | None) -> float:
    left_value = _normalized(left)
    right_value = _normalized(right)
    if not left_value or not right_value:
        return 0.0
    if left_value in right_value or right_value in left_value:
        return 0.9
    return SequenceMatcher(None, left_value, right_value).ratio()


def create_or_find_partner(name: str, code: str | None = None, *, force: bool = False) -> dict:
    name = str(name or "").strip()
    code = str(code or "").strip() or None
    if not name:
        raise ValueError("取引先名を入力してください。")
    if len(name) > 255:
        raise ValueError("取引先名は255文字以内で入力してください。")
    if code and len(code) > 191:
        raise ValueError("取引先コードは191文字以内で入力してください。")

    settings = freee_services.get_freee_common_settings()
    if not settings or not settings.get("company_id"):
        raise RuntimeError("freee共通設定で事業所を選択してください。")
    company_id = int(settings["company_id"])
    response = freee_services.freee_api_request(
        "GET",
        "/api/1/partners",
        params={"company_id": company_id, "limit": 3000},
    )
    partners = freee_services.freee_list_from_response(response, "partners")
    normalized_name = _normalized(name)
    normalized_code = _normalized(code)

    exact = next(
        (
            partner for partner in partners
            if _normalized(partner.get("name")) == normalized_name
            or (normalized_code and _normalized(partner.get("code")) == normalized_code)
        ),
        None,
    )
    if exact:
        return {"status": "existing", "partner": exact, "candidates": []}

    candidates = sorted(
        (
            {"partner": partner, "score": _similarity(name, partner.get("name"))}
            for partner in partners
        ),
        key=lambda row: row["score"],
        reverse=True,
    )
    candidates = [row["partner"] for row in candidates if row["score"] >= 0.72][:5]
    if candidates and not force:
        return {"status": "confirmation_required", "partner": None, "candidates": candidates}

    payload = {"company_id": company_id, "name": name}
    if code:
        payload["code"] = code
    created = freee_services.freee_api_request("POST", "/api/1/partners", json_body=payload)
    partner = created.get("partner") if isinstance(created, dict) else None
    if not partner and isinstance(created, dict) and created.get("id"):
        partner = created
    if not partner or not partner.get("id"):
        raise RuntimeError("freee取引先の作成結果を確認できませんでした。")
    return {"status": "created", "partner": partner, "candidates": []}
