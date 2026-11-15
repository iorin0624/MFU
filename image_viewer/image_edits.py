"""Non-destructive source retention for in-place image crops.

The active image keeps its path; its first, unedited version lives outside the
public image tree.  Legacy files use their virtual path as the backup key and
catalogue files use their stable UUID.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps


CROPPABLE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


class ImageEditError(ValueError):
    pass


def backup_path(root: Path, key: str, suffix: str) -> Path:
    relative = PurePosixPath(key.replace("\\", "/"))
    if relative.is_absolute() or not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ImageEditError("復元用の画像パスが不正です。")
    path = Path(root).joinpath(*relative.parts)
    return path if path.suffix.lower() == suffix.lower() else path.with_suffix(suffix.lower())


def ensure_private_backup_root(root: Path) -> None:
    """Keep retained originals readable only by the MFU service account."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)


def move_backup(root: Path, old_key: str, new_key: str, suffix: str, new_suffix: str | None = None) -> None:
    old = backup_path(root, old_key, suffix)
    if not old.is_file():
        return
    new = backup_path(root, new_key, new_suffix or suffix)
    ensure_private_backup_root(root)
    new.parent.mkdir(parents=True, exist_ok=True)
    if new.exists():
        raise ImageEditError("移動先に復元用の元画像が既にあります。")
    old.replace(new)


def remove_backup(root: Path, key: str, suffix: str) -> None:
    backup_path(root, key, suffix).unlink(missing_ok=True)


def _stage_file(path: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=".mfu-edit-", suffix=path.suffix, dir=path.parent)
    os.close(fd)
    return Path(name)


def _crop_image(source: Path, target: Path, rectangle: dict) -> tuple[int, int]:
    if source.suffix.lower() not in CROPPABLE_EXTENSIONS:
        raise ImageEditError("この形式は切り抜きに対応していません。")
    try:
        x, y, width, height = (float(rectangle[name]) for name in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ImageEditError("切り抜き範囲を確認してください。") from exc
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 and 0 < height <= 1):
        raise ImageEditError("切り抜き範囲を確認してください。")
    if x + width > 1.000001 or y + height > 1.000001:
        raise ImageEditError("切り抜き範囲が画像の外に出ています。")
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened)
        full_width, full_height = image.size
        left = round(x * full_width)
        top = round(y * full_height)
        right = min(full_width, round((x + width) * full_width))
        bottom = min(full_height, round((y + height) * full_height))
        if right - left < 2 or bottom - top < 2:
            raise ImageEditError("切り抜き範囲が小さすぎます。")
        cropped = image.crop((left, top, right, bottom))
        suffix = source.suffix.lower()
        if suffix in {".jpg", ".jpeg"}:
            cropped.convert("RGB").save(target, "JPEG", quality=95, subsampling=0)
        elif suffix == ".png":
            cropped.save(target, "PNG")
        else:
            cropped.save(target, "WEBP", quality=95)
        return cropped.size


def edit_image(
    active: Path, original: Path, *, rectangle: dict | None = None,
    restore: bool = False, after_replace=None,
) -> tuple[int, int]:
    """Replace active bytes atomically, preserving the first original.

    ``after_replace`` updates external metadata; if it fails, active bytes are
    rolled back.  The original backup is never served as an Explorer entry.
    """
    if not active.is_file() or active.suffix.lower() not in CROPPABLE_EXTENSIONS:
        raise ImageEditError("画像が見つからないか、切り抜き非対応の形式です。")
    if restore and not original.is_file():
        raise ImageEditError("復元できる元画像がありません。")
    original.parent.mkdir(parents=True, exist_ok=True)
    staged = _stage_file(active)
    previous = _stage_file(active)
    created_original = False
    try:
        shutil.copy2(active, previous)
        if restore:
            shutil.copy2(original, staged)
            with Image.open(staged) as image:
                dimensions = ImageOps.exif_transpose(image).size
        else:
            dimensions = _crop_image(active, staged, rectangle or {})
            if not original.exists():
                backup_stage = _stage_file(original)
                try:
                    shutil.copy2(active, backup_stage)
                    os.replace(backup_stage, original)
                    original.chmod(0o600)
                    created_original = True
                finally:
                    backup_stage.unlink(missing_ok=True)
        os.replace(staged, active)
        try:
            if after_replace:
                after_replace(active, dimensions)
        except Exception:
            os.replace(previous, active)
            if created_original:
                original.unlink(missing_ok=True)
            raise
        return dimensions
    finally:
        staged.unlink(missing_ok=True)
        previous.unlink(missing_ok=True)
