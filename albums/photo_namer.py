from PIL import Image
import piexif
from datetime import datetime

def get_datetime_from_image(file_storage):
    """
    ファイルのEXIFから撮影日時 (DateTimeOriginal) を取得。
    EXIFが無ければ現在時刻を返す。

    :param file_storage: FlaskのFileStorageオブジェクト
    :return: 'yyyymmdd_hhmmss' 形式の文字列
    """
    try:
        img = Image.open(file_storage.stream)
        exif_dict = piexif.load(img.info.get('exif', b''))
        dt_bytes = exif_dict["Exif"].get(piexif.ExifIFD.DateTimeOriginal)
        if dt_bytes:
            dt_str = dt_bytes.decode("utf-8")  # '2024:05:16 15:32:15'
            return dt_str.replace(":", "").replace(" ", "_")  # '20240516_153215'
    except Exception:
        pass
    return datetime.now().strftime("%Y%m%d_%H%M%S")
