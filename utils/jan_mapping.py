import json
import os
import re
from typing import Optional, Tuple, Dict, Any

MAP_FILE = "/mnt/mfu/data/jan_cupnoodles.json"
os.makedirs(os.path.dirname(MAP_FILE), exist_ok=True)

# 初期データ（ファイルが無い場合のみ参照）
_DEFAULT_MAP: Dict[str, Dict[str, Any]] = {
    # "4902105000017": {"label": "日清カップヌードル(ノーマル)", "seconds": 180},
}

def _load_map() -> Dict[str, Dict[str, Any]]:
    if os.path.exists(MAP_FILE):
        try:
            with open(MAP_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict):
                    return d
        except Exception:
            pass
    return _DEFAULT_MAP.copy()

def _save_map(d: Dict[str, Dict[str, Any]]) -> None:
    tmp = MAP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MAP_FILE)

def normalize_jan(jan: str) -> Optional[str]:
    """
    JANコード正規化:
      - 数字以外は全て除去
      - 8桁 (JAN-8 / EAN-8) または 13桁 (JAN-13 / EAN-13) のみ許可
    """
    if not jan:
        return None

    s = re.sub(r"\D+", "", jan)

    # 8桁 or 13桁のみ許可
    if len(s) in (8, 13):
        return s

    return None

# --- 既存互換：タイマー用の (label, seconds) を返す ---
def get_timer_by_jan(jan: str) -> Optional[Tuple[str, int]]:
    code = normalize_jan(jan)
    if not code:
        return None
    d = _load_map()
    hit = d.get(code)
    if not hit:
        return None
    try:
        label = str(hit.get("label", "")).strip()
        seconds = int(hit.get("seconds", 0))
        if label and seconds > 0:
            return label, seconds
    except Exception:
        return None
    return None

# --- 新規：情報そのまま返す（steps対応） ---
def get_info_by_jan(jan: str) -> Optional[Dict[str, Any]]:
    code = normalize_jan(jan)
    if not code:
        return None
    d = _load_map()
    info = d.get(code)
    if not info:
        return None
    try:
        out: Dict[str, Any] = {
            "label": str(info.get("label", "")).strip(),
            "seconds": int(info.get("seconds", 0)),
        }
        steps = info.get("steps")
        if isinstance(steps, list):
            out["steps"] = [str(s).strip() for s in steps if str(s).strip()]
        if not out["label"] or out["seconds"] <= 0:
            return None
        return out
    except Exception:
        return None

# --- 追加・更新 ---
def upsert_mapping(jan: str, label: str, seconds: int, steps: Optional[list] = None) -> bool:
    code = normalize_jan(jan)
    if not code or not label or seconds <= 0:
        return False
    d = _load_map()
    entry: Dict[str, Any] = {"label": label.strip(), "seconds": int(seconds)}
    if isinstance(steps, list):
        clean_steps = [str(s).strip() for s in steps if str(s).strip()]
        if clean_steps:
            entry["steps"] = clean_steps
    d[code] = entry
    _save_map(d)
    return True

# --- 一覧取得 ---
def list_mapping() -> Dict[str, Dict[str, Any]]:
    return _load_map()

# --- 削除 ---
def delete_mapping(jan: str) -> bool:
    code = normalize_jan(jan)
    if not code:
        return False
    d = _load_map()
    if code in d:
        del d[code]
        _save_map(d)
        return True
    return False
