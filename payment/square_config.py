# /mnt/mfu/app/payment/square_config.py

from pathlib import Path
import os

from dotenv import load_dotenv

# payment/.env を読む
BASE_DIR = Path(__file__).resolve().parent  # app/payment
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)

SQUARE_ENV = os.getenv("SQUARE_ENV", "SANDBOX").upper()
SQUARE_APP_ID = os.getenv("SQUARE_APP_ID")
SQUARE_LOCATION_ID = os.getenv("SQUARE_LOCATION_ID")
SQUARE_ACCESS_TOKEN = os.getenv("SQUARE_ACCESS_TOKEN")


def is_square_config_ready() -> bool:
    """カード登録に必要な値が全部そろっているか"""
    return bool(SQUARE_APP_ID and SQUARE_LOCATION_ID and SQUARE_ACCESS_TOKEN)


def create_square_client():
    """Square の Python SDK クライアントを返す"""
    from square.client import Client  # pip install squareup 済み前提

    environment = "sandbox" if SQUARE_ENV == "SANDBOX" else "production"

    return Client(
        access_token=SQUARE_ACCESS_TOKEN,
        environment=environment,
    )
