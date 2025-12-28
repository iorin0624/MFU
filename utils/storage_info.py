# app/utils/storage_info.py
import shutil

def get_storage_info(path="/mnt/mfu"):
    total, used, free = shutil.disk_usage(path)
    return {
        "total_gb": total // (2**30),
        "used_gb": used // (2**30),
        "free_gb": free // (2**30),
        "percent": round(used / total * 100, 1)
    }
