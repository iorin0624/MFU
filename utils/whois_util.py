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
    netname = data.get("netname") or ""
    org = data.get("org") or ""
    display_name = data.get("display_name") or ""
    if _is_generic_jpnic_name(netname) or _is_generic_jpnic_name(org) or _is_generic_jpnic_name(display_name):
        return None
    expires_at = data.get("expires_at")
    if expires_at and time.time() > expires_at:
        return None
    if expires_at is None:
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
        "network_name": "",
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
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        key_norm = key.replace(" ", "")
        if key_norm == "netname":
            info["netname"] = value
        elif key_norm == "networkname":
            info["network_name"] = value
        elif key_norm == "orgname":
            info["org"] = info["org"] or value
        elif key_norm == "organization":
            info["org"] = info["org"] or value
        elif key_norm == "owner" and not info["org"]:
            info["org"] = value
        elif key_norm == "descr" and not info["org"]:
            info["org"] = value
        elif key_norm == "country":
            info["country"] = value
        elif key_norm in ("as-name", "asname"):
            info["asname"] = value
        elif key_norm in ("origin", "originas"):
            info["asname"] = info["asname"] or value
    return info

def _is_generic_jpnic_name(name: str) -> bool:
    if not name:
        return False
    lowered = name.lower()
    return "japan network information center" in lowered or "jpnic" in lowered

def _pick_best_display_name(
    whois_info: dict,
    rdap_org: str,
    rdap_asname: str,
    rdap_name: str,
) -> str:
    candidates = [
        whois_info.get("network_name") or whois_info.get("netname") or "",
        whois_info.get("org") or "",
        rdap_org or "",
        rdap_asname or "",
        rdap_name or "",
    ]
    for candidate in candidates:
        if candidate and not _is_generic_jpnic_name(candidate):
            return candidate
    for candidate in candidates:
        if candidate:
            return candidate
    return ""

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

    rdap_name = ""
    rdap_org = ""
    rdap_country = ""
    rdap = _fetch_rdap(ip)
    if rdap:
        rdap_name, rdap_org, rdap_country = _extract_rdap_name(rdap)
        if rdap_name and not _is_generic_jpnic_name(rdap_name):
            info["netname"] = rdap_name
        if rdap_org:
            info["org"] = rdap_org
        if rdap_country:
            info["country"] = rdap_country
        info["source"] = "rdap"

    whois_text = ""
    try:
        whois_text = subprocess.check_output(["whois", ip], timeout=10).decode(errors="ignore")
    except Exception:
        whois_text = ""

    whois_info = {
        "netname": "",
        "network_name": "",
        "country": "",
        "org": "",
        "asname": "",
    }
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

    display_name = _pick_best_display_name(
        whois_info,
        rdap_org,
        info["asname"],
        rdap_name,
    )
    info["display_name"] = display_name or info["netname"]
    if display_name:
        info["netname"] = display_name
    if _is_generic_jpnic_name(info["org"]):
        info["org"] = ""

    is_failure = info["netname"] in ("不明", "") or _is_generic_jpnic_name(info["netname"])
    _save_cache(ip, info, FAIL_TTL_SEC if is_failure else SUCCESS_TTL_SEC)
    return info

if __name__ == "__main__":
    import sys

    def _run_selftest() -> None:
        whois_info = {
            "network_name": "Japan Network Information Center",
            "netname": "",
            "org": "SOME-ISP",
            "asname": "",
            "country": "",
        }
        picked = _pick_best_display_name(whois_info, "", "", "")
        assert picked == "SOME-ISP", f"unexpected display name: {picked}"

        ip = "203.0.113.1"
        cache_file = _cache_path(ip)
        try:
            cache_file.write_text(json.dumps({
                "netname": "JPNIC",
                "org": "",
                "display_name": "",
                "expires_at": time.time() + 60,
            }, ensure_ascii=False))
            assert _load_cache(ip, force_refresh=False) is None, "generic JPNIC cache should be ignored"
        finally:
            try:
                cache_file.unlink()
            except Exception:
                pass
        print("selftest ok")

    if "--selftest" in sys.argv:
        _run_selftest()
    else:
        target = sys.argv[1] if len(sys.argv) > 1 else "153.227.227.27"
        force = "--refresh" in sys.argv
        print(json.dumps(get_netinfo(target, force_refresh=force), ensure_ascii=False, indent=2))
