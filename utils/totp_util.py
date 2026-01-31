import pyotp
from app.utils.db import get_db as get_db_connection


def generate_totp_secret():
    return pyotp.random_base32()


def assign_otp_secret_to_user(username):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute(
        "SELECT totp_secret, totp_enabled FROM users WHERE username = %s", (username,)
    )
    row = cur.fetchone()
    if row and row.get("totp_secret"):
        return row["totp_secret"]

    new_secret = generate_totp_secret()
    cur.execute(
        "UPDATE users SET totp_secret = %s, totp_enabled = 1 WHERE username = %s",
        (new_secret, username),
    )
    conn.commit()
    return new_secret


def reset_totp_secret(username):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    new_secret = generate_totp_secret()
    cur.execute(
        "UPDATE users SET totp_secret = %s, totp_enabled = 1 WHERE username = %s",
        (new_secret, username),
    )
    conn.commit()
    return new_secret


def disable_totp_for_user(username):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "UPDATE users SET totp_secret = NULL, totp_enabled = 0 WHERE username = %s",
        (username,),
    )
    conn.commit()


def get_user_otp_secret(username):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT totp_secret, totp_enabled FROM users WHERE username = %s", (username,)
    )
    row = cur.fetchone()
    if not row:
        return None
    if row.get("totp_enabled") and row.get("totp_secret"):
        return row["totp_secret"]
    return None


def get_totp_status(username):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT totp_secret, totp_enabled FROM users WHERE username = %s", (username,)
    )
    row = cur.fetchone()
    if not row:
        return {"enabled": False, "has_secret": False}
    return {
        "enabled": bool(row.get("totp_enabled")),
        "has_secret": bool(row.get("totp_secret")),
    }
