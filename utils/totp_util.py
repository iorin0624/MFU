import pyotp
from app.utils.db import get_db as get_db_connection

def generate_otp_secret():
    return pyotp.random_base32()

def assign_otp_secret_to_user(username):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT otp_secret FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    if row and row['otp_secret']:
        return row['otp_secret']

    new_secret = generate_otp_secret()
    cur.execute("UPDATE users SET otp_secret = %s WHERE username = %s", (new_secret, username))
    conn.commit()
    return new_secret

def get_user_otp_secret(username):
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT otp_secret FROM users WHERE username = %s", (username,))
    row = cur.fetchone()
    return row['otp_secret'] if row else None
