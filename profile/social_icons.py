from __future__ import annotations

import io
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError


SOCIAL_ICON_ROOT = Path(os.environ.get("MFU_PROFILE_SOCIAL_ICON_ROOT", "/mnt/mfu/avatars/profile_social"))
MAX_ICON_BYTES = 5 * 1024 * 1024
MAX_ICON_PIXELS = 16_000_000
ICON_SIZE = 256
_ICON_FILE = re.compile(r"[0-9a-f]{32}\.webp\Z")
_BOOTSTRAP_ICON = re.compile(r"bi-[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def valid_icon_file(name: str | None) -> bool:
    return bool(_ICON_FILE.fullmatch(name or ""))


def valid_bootstrap_icon(name: str | None) -> bool:
    return bool(_BOOTSTRAP_ICON.fullmatch(name or ""))


def validate_social_link_fields(
    *, label: str, url: str, icon_mode: str, icon_name: str,
    has_custom_icon: bool,
) -> tuple[str, str, str, str | None]:
    label = label.strip()
    url = url.strip()
    icon_name = icon_name.strip()
    if not label or len(label) > 100:
        raise ValueError("表示名は1～100文字で入力してください。")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        parsed = None
        hostname = None
    if (
        not url or len(url) > 1000 or parsed is None or parsed.scheme not in {"http", "https"}
        or not hostname or any(character.isspace() or ord(character) < 32 for character in url)
        or parsed.username or parsed.password
    ):
        raise ValueError("URLはhttp://またはhttps://で始まる有効なURLを入力してください。")
    if icon_mode == "bootstrap":
        if not valid_bootstrap_icon(icon_name):
            raise ValueError("Bootstrap Iconsの名前を選択してください。")
        return label, url, icon_mode, icon_name
    if icon_mode == "custom":
        if not has_custom_icon:
            raise ValueError("独自画像を選択し、切り抜きを適用してください。")
        return label, url, icon_mode, None
    raise ValueError("アイコンの種類を選択してください。")


def save_social_icon(file_storage, *, root: Path = SOCIAL_ICON_ROOT) -> str:
    if file_storage is None or not getattr(file_storage, "filename", ""):
        raise ValueError("アイコン画像を選択してください。")
    raw = file_storage.stream.read(MAX_ICON_BYTES + 1)
    if len(raw) > MAX_ICON_BYTES:
        raise ValueError("アイコン画像は5MB以下にしてください。")
    if not raw:
        raise ValueError("アイコン画像を読み取れませんでした。")

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("アイコン画像はJPEG・PNG・WebPを選択してください。")
            if probe.width * probe.height > MAX_ICON_PIXELS:
                raise ValueError("アイコン画像の画素数が大きすぎます。")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as image:
            normalized = ImageOps.exif_transpose(image).convert("RGBA")
            square = ImageOps.fit(normalized, (ICON_SIZE, ICON_SIZE), method=Image.Resampling.LANCZOS)
            root.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}.webp"
            temporary = root / f".{name}.tmp"
            try:
                square.save(temporary, format="WEBP", quality=90, method=6)
                temporary.chmod(0o644)
                temporary.replace(root / name)
            finally:
                temporary.unlink(missing_ok=True)
            return name
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("アイコン画像を処理できませんでした。") from exc


def remove_social_icon(name: str | None, *, root: Path = SOCIAL_ICON_ROOT) -> None:
    if valid_icon_file(name):
        (root / name).unlink(missing_ok=True)
