# app/utils/whois_util.py
import subprocess
import re
import os
import hashlib
from pathlib import Path

WHOIS_CACHE_DIR = "/mnt/mfu/whois_cache"
os.makedirs(WHOIS_CACHE_DIR, exist_ok=True)

def get_netinfo(ip: str) -> dict:
    """IPからnetnameとcountryを取得（キャッシュ付き）"""
    key = hashlib.md5(ip.encode()).hexdigest()
    cache_file = Path(WHOIS_CACHE_DIR) / f"{key}.json"

    if cache_file.exists():
        try:
            import json
            return json.loads(cache_file.read_text())
        except:
            pass

    info = {"netname": "不明", "country": "不明"}

    try:
        result = subprocess.check_output(['whois', ip], timeout=10).decode(errors='ignore')
        for line in result.splitlines():
            line = line.strip()
            if line.lower().startswith("netname:"):
                info["netname"] = line.split(":", 1)[1].strip()
            elif line.lower().startswith("orgname:") and info["netname"] == "不明":
                info["netname"] = line.split(":", 1)[1].strip()
            elif line.lower().startswith("descr:") and info["netname"] == "不明":
                info["netname"] = line.split(":", 1)[1].strip()
            elif line.lower().startswith("country:"):
                info["country"] = line.split(":", 1)[1].strip()

        import json
        cache_file.write_text(json.dumps(info, ensure_ascii=False))

    except Exception as e:
        info["netname"] = f"取得エラー: {e}"

    return info
