from __future__ import annotations

import io
import os
import re
import uuid
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


PROFILE_AVATAR_ROOT = Path(os.environ.get("MFU_PROFILE_AVATAR_ROOT", "/mnt/mfu/avatars/profile"))
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
AVATAR_SIZE = 1024
_AVATAR_NAME = re.compile(r"[0-9a-f]{32}\.webp\Z")


def valid_avatar_name(name: str) -> bool:
    return bool(_AVATAR_NAME.fullmatch(name or ""))


def save_profile_avatar(file_storage, *, root: Path = PROFILE_AVATAR_ROOT) -> str:
    """Validate and save a square WebP avatar; never trust the upload MIME/name."""
    if file_storage is None or not getattr(file_storage, "filename", ""):
        raise ValueError("写真を選択してください。")

    raw = file_storage.stream.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("写真は8MB以下にしてください。")
    if not raw:
        raise ValueError("写真を読み取れませんでした。")

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("JPEG・PNG・WebPの写真を選択してください。")
            if probe.width * probe.height > MAX_IMAGE_PIXELS:
                raise ValueError("写真の画素数が大きすぎます。")
            probe.verify()
        with Image.open(io.BytesIO(raw)) as image:
            normalized = ImageOps.exif_transpose(image).convert("RGB")
            square = ImageOps.fit(
                normalized,
                (AVATAR_SIZE, AVATAR_SIZE),
                method=Image.Resampling.LANCZOS,
            )
            root.mkdir(parents=True, exist_ok=True)
            name = f"{uuid.uuid4().hex}.webp"
            temporary = root / f".{name}.tmp"
            target = root / name
            try:
                square.save(temporary, format="WEBP", quality=88, method=6)
                temporary.chmod(0o644)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            return name
    except ValueError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("写真を処理できませんでした。JPEG・PNG・WebPを選択してください。") from exc


def remove_profile_avatar(name: str | None, *, root: Path = PROFILE_AVATAR_ROOT) -> None:
    if name and valid_avatar_name(name):
        (root / name).unlink(missing_ok=True)
