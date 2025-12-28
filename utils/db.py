import time
import mysql.connector
from mysql.connector import errors
from config import MYSQL_CONFIG

def get_db(retries: int = 3, delay: float = 0.5):
    """
    MySQL接続ラッパ。
    瞬間的なネットワーク不調を吸収するため、ちょっとだけリトライを入れる。
    """
    last_err = None
    for attempt in range(retries):
        try:
            return mysql.connector.connect(**MYSQL_CONFIG)
        except errors.OperationalError as e:
            # 接続系エラー（2003, 2013など）はここに来る
            last_err = e
            if attempt == retries - 1:
                # 最後の1回も失敗したらそのまま投げる
                raise
            time.sleep(delay)
