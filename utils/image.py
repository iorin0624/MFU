from PIL import Image

def save_as_jpeg(input_stream, save_path, quality=80):
    try:
        img = Image.open(input_stream).convert("RGB")
        img.save(save_path, "JPEG", quality=quality)
        return True
    except Exception as e:
        print(f"画像変換失敗: {e}")
        return False
