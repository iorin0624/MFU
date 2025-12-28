import json
import os

PRESET_FILE = "/mnt/mfu/data/favorite_timers.json"

def load_presets():
    try:
        with open(PRESET_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_presets(presets):
    os.makedirs(os.path.dirname(PRESET_FILE), exist_ok=True)
    with open(PRESET_FILE, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)

def add_preset(label, seconds):
    presets = load_presets()
    if not any(p["label"] == label for p in presets):
        presets.append({"label": label, "seconds": seconds})
        save_presets(presets)
        return True
    return False

def delete_preset(label):
    presets = load_presets()
    updated = [p for p in presets if p["label"] != label]
    save_presets(updated)
    return len(presets) != len(updated)
