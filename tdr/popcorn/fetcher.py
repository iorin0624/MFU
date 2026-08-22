from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag


JST = timezone(timedelta(hours=9))
OFFICIAL_ORIGIN = "https://www.tokyodisneyresort.jp"
SOURCES = (
    ("tdl", "design", f"{OFFICIAL_ORIGIN}/tdl/food/popcorn.html"),
    ("tdl", "taste", f"{OFFICIAL_ORIGIN}/tdl/food/popcorn_taste.html"),
    ("tds", "design", f"{OFFICIAL_ORIGIN}/tds/food/popcorn.html"),
    ("tds", "taste", f"{OFFICIAL_ORIGIN}/tds/food/popcorn_taste.html"),
)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
        "MFU-TDR-Info/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.5",
}
TIMEOUT = (10, 15)
BROWSER_START_TIMEOUT = 30
BROWSER_PAGE_TIMEOUT = 60
_SESSION = requests.Session()
_SESSION.headers.update(HEADERS)
_LOGGER = logging.getLogger(__name__)


class PopcornFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchedPage:
    park: str
    kind: str
    url: str
    html: str
    status_code: int
    etag: str
    last_modified: str


@dataclass
class BrowserResponse:
    text: str
    status_code: int = 200
    headers: dict | None = None
    encoding: str = "utf-8"


class ChromiumGetter:
    """Fetch official pages with a normal Chromium process running on Xvfb."""

    def __init__(self) -> None:
        self.work_dir: Path | None = None
        self.process: subprocess.Popen | None = None
        self.ws = None
        self.message_id = 0
        self.debug_port = 0

    def __enter__(self):
        try:
            return self._start()
        except Exception:
            self.close()
            raise

    def _start(self):
        if not shutil.which("chromium") or not shutil.which("xvfb-run"):
            raise RuntimeError("Chromium/Xvfb is not installed.")

        self.work_dir = Path(tempfile.mkdtemp(prefix="mfu-tdr-browser-"))
        profile_dir = self.work_dir / "profile"
        runtime_dir = self.work_dir / "runtime"
        for path in (profile_dir, runtime_dir, self.work_dir / "config", self.work_dir / "cache", self.work_dir / "data"):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(runtime_dir, 0o700)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            self.debug_port = int(port_socket.getsockname()[1])

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(self.work_dir),
                "XDG_CONFIG_HOME": str(self.work_dir / "config"),
                "XDG_CACHE_HOME": str(self.work_dir / "cache"),
                "XDG_DATA_HOME": str(self.work_dir / "data"),
                "XDG_RUNTIME_DIR": str(runtime_dir),
            }
        )
        self.process = subprocess.Popen(
            [
                "xvfb-run",
                "-a",
                "-s",
                "-screen 0 1280x900x24 -nolisten tcp",
                "chromium",
                f"--user-data-dir={profile_dir}",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-crash-reporter",
                "--disable-breakpad",
                "--no-first-run",
                "--no-default-browser-check",
                "--password-store=basic",
                "--window-size=1280,900",
                f"--remote-debugging-port={self.debug_port}",
                "--remote-allow-origins=*",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )

        deadline = time.monotonic() + BROWSER_START_TIMEOUT
        target = None
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Chromium exited during startup ({self.process.returncode}).")
            try:
                targets = requests.get(f"http://127.0.0.1:{self.debug_port}/json", timeout=2).json()
                target = next(
                    (
                        item
                        for item in targets
                        if isinstance(item, dict)
                        and item.get("type") == "page"
                        and item.get("webSocketDebuggerUrl")
                    ),
                    None,
                )
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not target:
            raise RuntimeError("Chromium debug page was not ready.")

        import websocket

        self.ws = websocket.create_connection(str(target["webSocketDebuggerUrl"]), timeout=12)
        return self

    def _call(self, method: str, params: dict | None = None, timeout: int = 12) -> dict:
        if self.ws is None:
            raise RuntimeError("Chromium is not connected.")
        self.message_id += 1
        call_id = self.message_id
        self.ws.settimeout(timeout)
        self.ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != call_id:
                continue
            if message.get("error"):
                raise RuntimeError(str(message["error"]))
            return message.get("result") or {}

    def __call__(self, url: str) -> BrowserResponse:
        self._call("Page.navigate", {"url": url})
        deadline = time.monotonic() + BROWSER_PAGE_TIMEOUT
        last_state: dict = {}
        expression = """
            JSON.stringify({
                ready: document.readyState,
                href: location.href,
                title: document.title,
                hasContent: Boolean(document.querySelector('.listTextArea')),
                html: document.documentElement.outerHTML
            })
        """
        while time.monotonic() < deadline:
            result = self._call(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
            value = ((result.get("result") or {}).get("value"))
            if value:
                last_state = json.loads(value)
                html = str(last_state.get("html") or "")
                if (
                    last_state.get("ready") in {"interactive", "complete"}
                    and last_state.get("hasContent")
                    and len(html) >= 5000
                ):
                    return BrowserResponse(text=html, headers={})
            time.sleep(1)
        raise RuntimeError(
            "Chromium page load timed out: "
            f"title={last_state.get('title', '')!r} href={last_state.get('href', url)!r} "
            f"ready={last_state.get('ready', '')!r} has_content={last_state.get('hasContent', False)!r} "
            f"html_length={len(str(last_state.get('html') or ''))}"
        )

    def close(self) -> None:
        if self.ws is not None:
            try:
                self._call("Browser.close", timeout=3)
            except Exception:
                pass
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

        if self.process is not None and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except Exception:
                    pass
        self.process = None
        if self.work_dir is not None:
            shutil.rmtree(self.work_dir, ignore_errors=True)
            self.work_dir = None

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _official_url(value: str) -> str:
    resolved = urljoin(OFFICIAL_ORIGIN, value or "")
    parsed = urlparse(resolved)
    if parsed.scheme != "https" or parsed.netloc != "www.tokyodisneyresort.jp":
        return ""
    return resolved


def _official_image_url(value: str) -> str:
    resolved = urljoin(OFFICIAL_ORIGIN, value or "")
    parsed = urlparse(resolved)
    if parsed.scheme != "https" or parsed.netloc not in {
        "media1.tokyodisneyresort.jp",
        "media2.tokyodisneyresort.jp",
    }:
        return ""
    return resolved


def _parse_time_condition(raw_title: str) -> tuple[str, str, str, str]:
    title = _clean_text(raw_title)
    note_match = re.match(r"^[【〖]([^】〗]+)[】〗]\s*(.+)$", title)
    if not note_match:
        return title, "", "", ""
    note = note_match.group(1).strip()
    flavor = note_match.group(2).strip()
    start = ""
    end = ""
    start_match = re.search(r"(\d{1,2}):([0-5]\d)\s*[～〜~-]", note)
    end_match = re.search(r"[～〜~-]\s*(\d{1,2}):([0-5]\d)", note)
    if start_match:
        start = f"{int(start_match.group(1)):02d}:{start_match.group(2)}"
    if end_match:
        end = f"{int(end_match.group(1)):02d}:{end_match.group(2)}"
    return flavor, note, start, end


def parse_taste_page(html: str, park: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    flavors: list[dict] = []
    for heading in soup.select("h2.heading2"):
        raw_title = _clean_text(heading.get_text(" ", strip=True))
        if not raw_title.endswith("味"):
            continue
        flavor, time_note, available_from, available_until = _parse_time_condition(raw_title)
        section = heading.find_parent(class_="section")
        if section is None:
            continue

        offers: list[dict] = []
        for text_area in section.select(".listTextArea"):
            location_heading = text_area.select_one("h3.heading3")
            if location_heading is None:
                continue
            location = _clean_text(location_heading.get_text(" ", strip=True))
            all_text = _clean_text(text_area.get_text(" ", strip=True))
            area = all_text[: -len(location)].strip() if location and all_text.endswith(location) else ""
            anchor = text_area.find_parent("a", href=True)
            official_url = _official_url(anchor.get("href", "")) if anchor else ""
            offers.append(
                {
                    "area": area,
                    "location": location,
                    "official_url": official_url,
                    "time_note": time_note,
                    "available_from": available_from,
                    "available_until": available_until,
                }
            )

        if offers:
            flavors.append(
                {
                    "park": park,
                    "name": flavor,
                    "popcorn_type": "bb" if all(o["location"] == "ビッグポップ" for o in offers) else "regular",
                    "offers": offers,
                }
            )
    return flavors


def _price_from_text(value: str) -> int | None:
    matches = re.findall(r"[¥￥]\s*([\d,]+)", value or "")
    return int(matches[-1].replace(",", "")) if matches else None


def _product_id(href: str) -> str:
    match = re.search(r"/food/(\d+)/?", href or "")
    return match.group(1) if match else ""


def _parse_product_offers(description: Tag) -> list[dict]:
    offers: list[dict] = []
    current_flavor = ""
    for child in description.children:
        if not isinstance(child, Tag):
            continue
        classes = set(child.get("class") or [])
        if child.name == "p" and "bold" in classes:
            raw_flavor = _clean_text(child.get_text(" ", strip=True)).lstrip("#＃")
            current_flavor = _parse_time_condition(raw_flavor)[0]
            continue
        if "listType-disc" not in classes or not current_flavor:
            continue
        last_offer: dict | None = None
        for li in child.select("li"):
            text = _clean_text(li.get_text(" ", strip=True))
            if not text:
                continue
            if re.search(r"\d{4}年\d{1,2}月\d{1,2}日|[〜～]", text):
                if last_offer is not None:
                    last_offer["period"] = text
                continue
            last_offer = {"flavor": current_flavor, "location": text, "period": ""}
            offers.append(last_offer)
    return offers


def parse_design_page(html: str, park: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    products: list[dict] = []
    seen: set[str] = set()
    for anchor in soup.select('a[href*="/food/"]'):
        heading = anchor.select_one(".listTextArea h3.heading3")
        price_node = anchor.select_one(".listTextArea .text")
        description = anchor.select_one(".listTextArea .description")
        if heading is None or price_node is None or description is None:
            continue
        title = _clean_text(heading.get_text(" ", strip=True))
        if "バケット" not in title and "スーベニアポップコーンケース" not in title:
            continue
        product_id = _product_id(anchor.get("href", ""))
        if not product_id or product_id in seen:
            continue
        seen.add(product_id)
        price_text = _clean_text(price_node.get_text(" ", strip=True))
        price = _price_from_text(price_text)
        if price is None:
            continue
        notice = re.sub(r"[¥￥]\s*[\d,]+.*$", "", price_text).strip()
        image = anchor.find("img")
        image_url = _official_image_url(
            (image.get("src") or image.get("data-src") or "") if image else ""
        )
        products.append(
            {
                "id": product_id,
                "parks": [park],
                "title": title,
                "product_type": "bucket" if "バケット" in title else "case",
                "popcorn_type": "bb" if title.startswith("BBポップコーン") else "regular",
                "price": price,
                "is_new": anchor.select_one(".tagArea .new") is not None,
                "notice": notice,
                "official_url": _official_url(anchor.get("href", "")),
                "image_url": image_url,
                "offers": _parse_product_offers(description),
            }
        )
    return products


def _merge_products(products: list[dict], flavor_rows: list[dict]) -> list[dict]:
    location_meta: dict[tuple[str, str], dict] = {}
    for row in flavor_rows:
        for offer in row["offers"]:
            location_meta[(row["park"], offer["location"])] = {
                "area": offer["area"],
                "official_url": offer["official_url"],
                "time_note": offer["time_note"],
            }

    merged: dict[str, dict] = {}
    for product in products:
        product_id = product["id"]
        park = product["parks"][0]
        if product_id not in merged:
            merged[product_id] = product
        else:
            current = merged[product_id]
            current["parks"] = sorted(set(current["parks"] + product["parks"]))
            current["is_new"] = bool(current["is_new"] or product["is_new"])
            existing = {(o["park"], o["flavor"], o["location"], o.get("period", "")) for o in current["offers"]}
            for offer in product["offers"]:
                key = (park, offer["flavor"], offer["location"], offer.get("period", ""))
                if key not in existing:
                    current["offers"].append({**offer, "park": park})
                    existing.add(key)
            continue

        product["offers"] = [{**offer, "park": park} for offer in product["offers"]]

    result = []
    for product in merged.values():
        for offer in product["offers"]:
            meta = location_meta.get((offer["park"], offer["location"]), {})
            offer["area"] = meta.get("area", "")
            offer["location_url"] = meta.get("official_url", "")
            offer["time_note"] = meta.get("time_note", "")
        product["parks"] = sorted(set(product["parks"]))
        product["offers"].sort(key=lambda o: (o["park"], o["flavor"], o["location"]))
        result.append(product)
    return sorted(result, key=lambda p: (-int(p["is_new"]), p["price"], p["id"]))


def _validate_page(page: FetchedPage, parsed_count: int) -> None:
    title_marker = "ポップコーン"
    if title_marker not in page.html:
        raise PopcornFetchError(f"{page.park}/{page.kind}: ページ構造を確認できません")
    minimum = 5 if page.kind == "taste" else 3
    if parsed_count < minimum:
        raise PopcornFetchError(
            f"{page.park}/{page.kind}: 解析件数が少なすぎます ({parsed_count}件、最低{minimum}件)"
        )


def _default_get(url: str):
    return _SESSION.get(url, timeout=TIMEOUT)


def _fetch_all_pages_with_getter(
    getter: Callable[[str], object],
    *,
    transport: str,
) -> tuple[dict, dict]:
    pages: list[FetchedPage] = []
    source_meta: list[dict] = []
    flavor_rows: list[dict] = []
    product_rows: list[dict] = []

    for park, kind, url in SOURCES:
        try:
            response = getter(url)
        except Exception as exc:
            raise PopcornFetchError(f"{park}/{kind}: 取得に失敗しました: {exc}") from exc
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code != 200:
            raise PopcornFetchError(f"{park}/{kind}: HTTP {status_code}")
        encoding = getattr(response, "encoding", None)
        if not encoding:
            setattr(response, "encoding", "utf-8")
        html = getattr(response, "text", "") or ""
        headers = getattr(response, "headers", {}) or {}
        page = FetchedPage(
            park=park,
            kind=kind,
            url=url,
            html=html,
            status_code=status_code,
            etag=_clean_text(headers.get("ETag", "")),
            last_modified=_clean_text(headers.get("Last-Modified", "")),
        )
        pages.append(page)
        parsed = parse_taste_page(html, park) if kind == "taste" else parse_design_page(html, park)
        _validate_page(page, len(parsed))
        if kind == "taste":
            flavor_rows.extend(parsed)
        else:
            product_rows.extend(parsed)
        source_meta.append(
            {
                "park": park,
                "kind": kind,
                "url": url,
                "status_code": status_code,
                "etag": page.etag,
                "last_modified": page.last_modified,
                "parsed_count": len(parsed),
                "transport": transport,
            }
        )

    products = _merge_products(product_rows, flavor_rows)
    dataset = {
        "schema_version": 1,
        "fetched_at": datetime.now(JST).isoformat(timespec="seconds"),
        "flavors": sorted(flavor_rows, key=lambda r: (r["name"], r["park"])),
        "products": products,
        "sources": source_meta,
        "summary": {
            "flavor_count": len(flavor_rows),
            "offer_count": sum(len(row["offers"]) for row in flavor_rows),
            "product_count": len(products),
        },
    }
    content_payload = {
        "schema_version": dataset["schema_version"],
        "flavors": dataset["flavors"],
        "products": dataset["products"],
    }
    canonical = json.dumps(content_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    metadata = {
        "content_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "source_status": source_meta,
        "counts": dataset["summary"],
    }
    return dataset, metadata


def fetch_all_pages(get: Callable[[str], object] | None = None) -> tuple[dict, dict]:
    if get is not None:
        return _fetch_all_pages_with_getter(get, transport="custom")

    try:
        return _fetch_all_pages_with_getter(_default_get, transport="http")
    except PopcornFetchError as http_error:
        _LOGGER.warning("TDR direct HTTP fetch failed; retrying with Chromium: %s", http_error)

    try:
        with ChromiumGetter() as browser_get:
            return _fetch_all_pages_with_getter(browser_get, transport="chromium")
    except Exception as browser_error:
        raise PopcornFetchError(f"Chromium取得にも失敗しました: {browser_error}") from browser_error
