import sys
import os
sys.path.append('/mnt/mfu')

from app.utils.db import get_db

def get_switchbot_token():
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT token FROM switchbot_tokens ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    db.close()

    if not row:
        raise Exception("❌ トークンがDBに見つかりません")
    return row['token']

def main():
    token = get_switchbot_token()
    headers = {
        "Authorization": token,
        "Content-Type": "application/json"
    }

    print("🔍 デバイス一覧を取得中...")
    try:
        res = requests.get("https://api.switch-bot.com/v1.0/devices", headers=headers)
        res.raise_for_status()
        data = res.json()
        print("✅ 応答:")
        print(data)
    except Exception as e:
        print(f"❌ APIエラー: {e}")

if __name__ == "__main__":
    main()
