import os
import uuid
import json
import bcrypt
from getpass import getpass

ALBUM_ROOT = '/mnt/maildata/mfu/albums'

def main():
    album_name = input("アルバム名（例：2025春オフ会）: ").strip()
    password = getpass("参加者用パスワードを入力: ").encode('utf-8')
    password_confirm = getpass("パスワード再入力: ").encode('utf-8')

    if password != password_confirm:
        print("❌ パスワードが一致しません")
        return

    album_id = str(uuid.uuid4())
    album_path = os.path.join(ALBUM_ROOT, album_id)
    os.makedirs(album_path, exist_ok=True)

    password_hash = bcrypt.hashpw(password, bcrypt.gensalt()).decode('utf-8')
    meta = {
        "album_name": album_name,
        "password_hash": password_hash,
        "children": []
    }

    with open(os.path.join(album_path, 'meta.json'), 'w') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"✅ アルバム作成完了！")
    print(f"📁 UUID: {album_id}")
    print(f"🌐 アクセスURL例: http://[IP]:5000/album/access/{album_id}")

if __name__ == '__main__':
    main()
