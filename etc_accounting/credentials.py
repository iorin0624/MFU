from __future__ import annotations

import base64
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


CREDENTIALS_ROOT = Path(os.environ.get("ETC_CREDENTIALS_ROOT", "/mnt/mfu/secure/etc_accounting"))
CREDENTIALS_FILE = Path(os.environ.get("ETC_CREDENTIALS_FILE", str(CREDENTIALS_ROOT / "credentials.enc")))
LOCK_FILE = Path(os.environ.get("ETC_BROWSER_LOCK_FILE", str(CREDENTIALS_ROOT / "browser.lock")))
FAILURE_FILE = Path(os.environ.get("ETC_LOGIN_FAILURE_FILE", str(CREDENTIALS_ROOT / "login_failure.json")))


def _ensure_root() -> None:
    CREDENTIALS_ROOT.mkdir(parents=True, exist_ok=True)
    os.chmod(CREDENTIALS_ROOT, 0o700)
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    FAILURE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _fernet() -> Fernet:
    secret = (os.environ.get("ETC_CREDENTIALS_KEY") or os.environ.get("SECRET_KEY") or "").encode("utf-8")
    if not secret:
        raise RuntimeError("ETC認証情報の暗号化キーが設定されていません。")
    digest = hashlib.sha256(b"mfu:etc-accounting:credentials:v1\0" + secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def save_credentials(login_id: str, password: str) -> None:
    login_id = (login_id or "").strip()
    if not login_id or not password:
        raise ValueError("ETCのユーザーIDとパスワードを入力してください。")
    _ensure_root()
    encrypted = _fernet().encrypt(
        json.dumps({"login_id": login_id, "password": password}, ensure_ascii=False).encode("utf-8")
    )
    temporary = CREDENTIALS_FILE.with_suffix(CREDENTIALS_FILE.suffix + ".tmp")
    temporary.write_bytes(encrypted)
    os.chmod(temporary, 0o600)
    temporary.replace(CREDENTIALS_FILE)
    os.chmod(CREDENTIALS_FILE, 0o600)
    clear_login_failure()


def load_credentials() -> dict | None:
    if not CREDENTIALS_FILE.is_file():
        return None
    try:
        decrypted = _fernet().decrypt(CREDENTIALS_FILE.read_bytes())
        data = json.loads(decrypted.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("ETC認証情報を復号できません。再登録してください。") from exc
    login_id = str(data.get("login_id") or "").strip()
    password = str(data.get("password") or "")
    if not login_id or not password:
        raise RuntimeError("保存されたETC認証情報が不完全です。再登録してください。")
    return {"login_id": login_id, "password": password}


def delete_credentials() -> None:
    try:
        CREDENTIALS_FILE.unlink()
    except FileNotFoundError:
        pass
    clear_login_failure()


def credentials_status() -> dict:
    credentials = load_credentials()
    return {
        "configured": bool(credentials),
        "login_id": credentials["login_id"] if credentials else "",
        "login_blocked": auto_login_failure() is not None,
    }


def _credentials_fingerprint() -> str:
    if not CREDENTIALS_FILE.is_file():
        return ""
    return hashlib.sha256(CREDENTIALS_FILE.read_bytes()).hexdigest()


def record_login_failure() -> None:
    _ensure_root()
    data = {
        "credentials_fingerprint": _credentials_fingerprint(),
        "failed_at": datetime.now(timezone.utc).isoformat(),
    }
    temporary = FAILURE_FILE.with_suffix(FAILURE_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(FAILURE_FILE)
    os.chmod(FAILURE_FILE, 0o600)


def auto_login_failure() -> dict | None:
    if not FAILURE_FILE.is_file():
        return None
    try:
        data = json.loads(FAILURE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"failed_at": ""}
    if data.get("credentials_fingerprint") != _credentials_fingerprint():
        clear_login_failure()
        return None
    return data


def clear_login_failure() -> None:
    try:
        FAILURE_FILE.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def etc_browser_lock():
    _ensure_root()
    LOCK_FILE.touch(mode=0o600, exist_ok=True)
    os.chmod(LOCK_FILE, 0o600)
    with LOCK_FILE.open("r+", encoding="utf-8") as lock_file:
        try:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except ImportError:
            fcntl = None
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
