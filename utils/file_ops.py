# app/utils/file_ops.py
import os
import re
import subprocess
from pathlib import Path
from PIL import Image, ImageOps  # 修正: ImageOps を追加インポート

def sanitize_filename(name, existing):
    name = Path(name).name
    name = re.sub(r'[\\/:*?"<>|]', '_', name)
    base, ext = os.path.splitext(name)
    counter = 1
    new_name = name
    while new_name in existing:
        new_name = f"{base}_{counter}{ext}"
        counter += 1
    return new_name

def generate_thumbnail(save_path, thumb_path, size=(300, 300), quality=40):
    try:
        with Image.open(save_path) as img:
            # ExifのOrientationに従って回転を補正
            img = ImageOps.exif_transpose(img)
            img.thumbnail(size)
            img.convert("RGB").save(thumb_path, "JPEG", quality=quality, optimize=True)
    except Exception as e:
        print(f"サムネイル生成失敗: {save_path} -> {e}")

def create_zip(zip_path, files):
    subprocess.run(
        ["7z", "a", "-mx=1", "-mcu=on", "-mcp=932", zip_path] + files,
        stdout=subprocess.DEVNULL
    )

# app/utils/db.py
import mysql.connector
from config import MYSQL_CONFIG

def get_db():
    return mysql.connector.connect(**MYSQL_CONFIG)


# app/utils/image.py
from PIL import Image

def save_as_jpeg(input_stream, save_path, quality=80):
    try:
        img = Image.open(input_stream).convert("RGB")
        img.save(save_path, "JPEG", quality=quality)
        return True
    except Exception as e:
        print(f"画像変換失敗: {e}")
        return False
