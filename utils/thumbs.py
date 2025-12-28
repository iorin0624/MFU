# utils/thumbs.py

import os
import json
from PIL import Image

THUMB_QUEUE = '/mnt/mfu/thumb_queue'

# 対応するルートディレクトリ（album or upload）
ALBUM_ROOTS = {
    "album": "/mnt/mfu/mfu_albums",
    "upload": "/mnt/mfu/uploads"
}

ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.heic'}

def allowed_file(filename):
    return any(filename.lower().endswith(ext) for ext in ALLOWED_EXTENSIONS)

def enqueue_thumb_job(mode, album_id, child_id):
    job = {
        "mode": mode,
        "album_id": album_id,
        "child_id": child_id
    }
    queue_path = os.path.join(THUMB_QUEUE, f"{album_id}_{child_id}.json")
    os.makedirs(THUMB_QUEUE, exist_ok=True)
    with open(queue_path, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)

def get_files_with_thumbs(mode, album_id, child_id):
    import os

    ALBUM_ROOTS = {
        "album": "/mnt/mfu/mfu_albums",
        "upload": "/mnt/mfu/uploads"
    }

    def allowed_file(filename):
        return any(filename.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.heic'])

    if mode not in ALBUM_ROOTS:
        print(f"[ERROR] get_files_with_thumbs: 未知のmode: {mode}")
        return []

    root = ALBUM_ROOTS[mode]
    path = os.path.join(root, album_id, child_id)
    thumb_path = os.path.join(path, 'thumbs')

    files = []
    if not os.path.exists(path):
        return files

    for f in sorted(os.listdir(path)):
        if allowed_file(f):
            thumb = f"{os.path.splitext(f)[0]}.jpg"
            thumb_full = os.path.join(thumb_path, thumb)

            if os.path.exists(thumb_full):
                if mode == "album":
                    thumb_url = f"/album/{album_id}/thumb/{child_id}/{thumb}"
                elif mode == "upload":
                    thumb_url = f"/upload/thumbs/{album_id}/{child_id}/{thumb}"  # ← upload側未定義ならこのままでOK
            else:
                thumb_url = None

            files.append({
                "name": f,
                "thumb": thumb_url
            })

    return files
