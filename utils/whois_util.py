# app/utils/whois_util.py
import subprocess
import os
import hashlib
import json
import time
import ipaddress
from typing import Optional
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

WHOIS_CACHE_DIR = "/mnt/mfu/whois_cache"
os.makedirs(WHOIS_CACHE_DIR, exist_ok=True)

SUCCESS_TTL_SEC = 86400 * 7
FAIL_TTL_SEC = 3600

def _cache_path(ip: str) -> Path:
    key = hashlib.md5(ip.encode()).hexdigest()
    return Path(WHOIS_CACHE_DIR) / f"{key}.json"

def _load_cache(ip: str, *, force_refresh: bool) -> Optional[dict]:
    if force_refresh:
        return None
    cache_file = _cache_path(ip)
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
    except Exception:
        return None
    expires_at = data.get("expires_at")
    if expires_at and time.time() > expires_at:
        return None
    if expires_at is None:
        netname = data.get("netname")
        if netname and netname != "不明" and not str(netname).startswith("取得エラー"):
            return data
        return None
    return data

def _save_cache(ip: str, info: dict, ttl: int) -> None:
    cache_file = _cache_path(ip)
    data = dict(info)
    data["fetched_at"] = time.time()
    data["expires_at"] = time.time() + ttl
    try:
        cache_file.write_text(json.dumps(data, ensure_ascii=False))
    except Exception:
        pass

def _fetch_rdap(ip: str) -> Optional[dict]:
    url = f"https://rdap.org/ip/{ip}"
    req = Request(url, headers={"User-Agent": "MFU/rdap"})
    try:
        with urlopen(req, timeout=8) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (URLError, HTTPError, json.JSONDecodeError, TimeoutError):
        return None

def _extract_rdap_name(rdap: dict) -> tuple[str, str, str]:
    if not rdap:
        return "", "", ""
    name = (rdap.get("name") or "").strip()
    country = (rdap.get("country") or rdap.get("countryCode") or "").strip()

    org = ""
    entities = rdap.get("entities") or []
    for ent in entities:
        vcard = ent.get("vcardArray") if isinstance(ent, dict) else None
        if not (isinstance(vcard, list) and len(vcard) >= 2):
            continue
        for item in vcard[1]:
            if len(item) < 4:
                continue
            key = item[0]
            val = item[3]
            if key in ("fn", "org") and isinstance(val, str):
                org = val.strip()
                break
        if org:
            break

    if not org:
        remarks = rdap.get("remarks") or []
        for remark in remarks:
            desc = remark.get("description") if isinstance(remark, dict) else None
            if isinstance(desc, list):
                for d in desc:
                    if isinstance(d, str) and d.strip():
                        org = d.strip()
                        break
            if org:
                break

    return name, org, country

def _extract_whois_info(text: str) -> dict:
    info = {
        "netname": "",
        "country": "",
        "org": "",
        "asname": "",
    }
    if not text:
        return info
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("netname:"):
            info["netname"] = line.split(":", 1)[1].strip()
        elif lower.startswith("orgname:"):
            info["org"] = info["org"] or line.split(":", 1)[1].strip()
        elif lower.startswith("organization:"):
            info["org"] = info["org"] or line.split(":", 1)[1].strip()
        elif lower.startswith("owner:") and not info["org"]:
            info["org"] = line.split(":", 1)[1].strip()
        elif lower.startswith("descr:") and not info["org"]:
            info["org"] = line.split(":", 1)[1].strip()
        elif lower.startswith("country:"):
            info["country"] = line.split(":", 1)[1].strip()
        elif lower.startswith("as-name:") or lower.startswith("asname:"):
            info["asname"] = line.split(":", 1)[1].strip()
        elif lower.startswith("origin:") or lower.startswith("originas:"):
            info["asname"] = info["asname"] or line.split(":", 1)[1].strip()
    return info

def get_netinfo(ip: str, *, force_refresh: bool = False) -> dict:
    """IPからnetnameとcountryを取得（キャッシュ付き）"""
    cached = _load_cache(ip, force_refresh=force_refresh)
    if cached:
        return cached

    try:
        ip_obj = ipaddress.ip_address(ip)
        is_ipv6 = ip_obj.version == 6
    except ValueError:
        is_ipv6 = False

    info = {
        "netname": "不明",
        "country": "不明",
        "org": "",
        "asname": "",
        "source": "",
    }

    rdap = _fetch_rdap(ip)
    if rdap:
        name, org, country = _extract_rdap_name(rdap)
        if name:
            info["netname"] = name
        if org:
            info["org"] = org
        if country:
            info["country"] = country
        info["source"] = "rdap"

    whois_text = ""
    try:
        whois_text = subprocess.check_output(["whois", ip], timeout=10).decode(errors="ignore")
    except Exception:
        whois_text = ""

    if whois_text:
        whois_info = _extract_whois_info(whois_text)
        if info["netname"] == "不明" and whois_info["netname"]:
            info["netname"] = whois_info["netname"]
        if not info["org"] and whois_info["org"]:
            info["org"] = whois_info["org"]
        if not info["asname"] and whois_info["asname"]:
            info["asname"] = whois_info["asname"]
        if info["country"] == "不明" and whois_info["country"]:
            info["country"] = whois_info["country"]
        if not info["source"]:
            info["source"] = "whois"

    if info["netname"] == "不明" and not is_ipv6:
        if info["org"]:
            info["netname"] = info["org"]
        elif info["asname"]:
            info["netname"] = info["asname"]

    if is_ipv6:
        info["display_name"] = info["netname"]

    is_failure = info["netname"] in ("不明", "")
    _save_cache(ip, info, FAIL_TTL_SEC if is_failure else SUCCESS_TTL_SEC)
    return info

if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "153.227.227.27"
    force = "--refresh" in sys.argv
    print(json.dumps(get_netinfo(target, force_refresh=force), ensure_ascii=False, indent=2))
