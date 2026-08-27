from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError


MAX_IMAGE_PIXELS = 200_000_000
JPEG_QUALITY = 95
HEIF_BRANDS = {
    b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis",
    b"hevm", b"hevs", b"mif1", b"msf1",
}


class IOSUploadImageError(ValueError):
    pass


def looks_like_heif(header: bytes) -> bool:
    if len(header) < 12 or header[4:8] != b"ftyp":
        return False
    return header[8:12] in HEIF_BRANDS or any(
        header[offset:offset + 4] in HEIF_BRANDS
        for offset in range(16, min(len(header), 128), 4)
    )


def _jpeg_name(filename: str) -> str:
    base = Path(filename or "photo").stem.strip() or "photo"
    base = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", base).strip(" .") or "photo"
    return f"{base}.jpg"


def _reserve_name(directory: str, filename: str) -> tuple[str, str]:
    root, ext = os.path.splitext(filename)
    for number in range(0, 100_000):
        candidate = filename if number == 0 else f"{root}({number}){ext}"
        path = os.path.join(directory, candidate)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
            os.close(fd)
            return path, candidate
        except FileExistsError:
            continue
    raise IOSUploadImageError("保存ファイル名を確保できませんでした。")


def _to_srgb(image: Image.Image) -> Image.Image:
    icc = image.info.get("icc_profile")
    if icc:
        try:
            source = ImageCms.ImageCmsProfile(__import__("io").BytesIO(icc))
            target = ImageCms.createProfile("sRGB")
            return ImageCms.profileToProfile(image, source, target, outputMode="RGB")
        except Exception:
            pass
    return image.convert("RGB")


def convert_heif_to_jpeg(source_path: str, destination_dir: str, original_filename: str) -> tuple[str, str]:
    """Decode a HEIC/HEIF file and atomically publish a verified JPEG."""
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    final_path, saved_name = _reserve_name(destination_dir, _jpeg_name(original_filename))
    temp_path = ""
    try:
        fd, temp_path = tempfile.mkstemp(prefix=".ios-heic-", suffix=".jpg", dir=destination_dir)
        os.close(fd)
        try:
            from pillow_heif import register_heif_opener
        except Exception:  # The production host already provides ImageMagick/libheif.
            magick = shutil.which("magick")
            if not magick:
                raise IOSUploadImageError("HEIC変換機能を利用できません。")
            completed = subprocess.run(
                [
                    magick,
                    "-limit", "memory", "512MiB",
                    "-limit", "map", "1GiB",
                    "-limit", "disk", "2GiB",
                    f"{source_path}[0]",
                    "-auto-orient",
                    "-colorspace", "sRGB",
                    "-strip",
                    "-quality", str(JPEG_QUALITY),
                    temp_path,
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "").strip()
                raise IOSUploadImageError(
                    f"HEIC画像をJPEGへ変換できませんでした。{detail[:300]}"
                )
        else:
            register_heif_opener()
            try:
                with Image.open(source_path) as opened:
                    opened.load()
                    width, height = opened.size
                    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                        raise IOSUploadImageError("画像サイズが安全上の上限を超えています。")
                    image = ImageOps.exif_transpose(opened)
                    image = _to_srgb(image)
                    image.save(
                        temp_path,
                        format="JPEG",
                        quality=JPEG_QUALITY,
                        optimize=True,
                        progressive=True,
                        dpi=opened.info.get("dpi", (72, 72)),
                    )
            except (UnidentifiedImageError, OSError) as exc:
                raise IOSUploadImageError("HEIC画像が破損しているか、読み取れません。") from exc

        with Image.open(temp_path) as check:
            if (
                check.format != "JPEG"
                or check.width <= 0
                or check.height <= 0
                or check.width * check.height > MAX_IMAGE_PIXELS
            ):
                raise IOSUploadImageError("HEIC画像のJPEG変換結果を検証できませんでした。")
            check.verify()

        os.replace(temp_path, final_path)
        temp_path = ""
        os.chmod(final_path, 0o640)
        return final_path, saved_name
    except Exception:
        try:
            os.remove(final_path)
        except OSError:
            pass
        raise
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
