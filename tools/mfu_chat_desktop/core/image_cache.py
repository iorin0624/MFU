from __future__ import annotations

import hashlib
import os
from urllib.parse import urljoin

import requests

from config import CONFIG


class ImageCache:
    def __init__(self, session: requests.Session, base_url: str) -> None:
        self.session = session
        self.base_url = base_url
        self.cache_dir = os.path.join(CONFIG.config_dir, "image_cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    def fetch(self, url: str) -> str | None:
        if not url:
            return None
        absolute = url if url.startswith("http") else urljoin(self.base_url + "/", url.lstrip("/"))
        name = hashlib.sha256(absolute.encode("utf-8")).hexdigest()
        path = os.path.join(self.cache_dir, name)
        if os.path.exists(path):
            return path
        try:
            res = self.session.get(absolute, timeout=30)
            res.raise_for_status()
            with open(path, "wb") as fh:
                fh.write(res.content)
            return path
        except Exception:
            return None
