from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


ROOT = Path(os.environ.get("INSTAGRAM_AUTH_DIR", "/mnt/mfu/secure/instagram_auth"))
CREDENTIALS_FILE = Path(
    os.environ.get("INSTAGRAM_CREDENTIALS_FILE", str(ROOT / "credentials.enc"))
)


def _fernet() -> Fernet:
    secret = (
        os.environ.get("INSTAGRAM_CREDENTIALS_KEY")
        or os.environ.get("SECRET_KEY")
        or ""
    ).encode("utf-8")
    if not secret:
        raise RuntimeError("Instagram認証情報の暗号化キーが設定されていません。")
    digest = hashlib.sha256(b"mfu:instagram:credentials:v1\0" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def save_credentials(login_id: str, password: str) -> None:
    login_id = str(login_id or "").strip()
    password = str(password or "")
    if not login_id or not password:
        raise ValueError("InstagramのログインIDとパスワードを入力してください。")
    ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(ROOT, 0o700)
    encrypted = _fernet().encrypt(
        json.dumps(
            {"login_id": login_id, "password": password},
            ensure_ascii=False,
        ).encode("utf-8")
    )
    temporary = CREDENTIALS_FILE.with_suffix(CREDENTIALS_FILE.suffix + ".tmp")
    temporary.write_bytes(encrypted)
    os.chmod(temporary, 0o600)
    temporary.replace(CREDENTIALS_FILE)
    os.chmod(CREDENTIALS_FILE, 0o600)


def load_credentials() -> dict | None:
    if not CREDENTIALS_FILE.is_file():
        return None
    try:
        data = json.loads(_fernet().decrypt(CREDENTIALS_FILE.read_bytes()).decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Instagram認証情報を復号できません。再登録してください。") from exc
    login_id = str(data.get("login_id") or "").strip()
    password = str(data.get("password") or "")
    if not login_id or not password:
        raise RuntimeError("保存されたInstagram認証情報が不完全です。")
    return {"login_id": login_id, "password": password}


def credentials_status() -> dict:
    credentials = load_credentials()
    return {
        "configured": bool(credentials),
        "login_id": credentials["login_id"] if credentials else "",
    }


def delete_credentials() -> None:
    CREDENTIALS_FILE.unlink(missing_ok=True)
