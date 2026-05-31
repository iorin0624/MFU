from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv
from platformdirs import user_config_dir


load_dotenv()


@dataclass
class AppConfig:
    base_url: str = os.getenv("MFU_BASE_URL", "https://mfu.iori0624.jp").rstrip("/")
    socket_path: str = os.getenv("MFU_SOCKET_PATH", "/socket.io")
    app_name: str = os.getenv("APP_NAME", "MFU Chat Desktop")
    config_dir: str = user_config_dir("MFU Chat Desktop", "MFU")


CONFIG = AppConfig()
