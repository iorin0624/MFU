from __future__ import annotations

import logging
import os

from config import CONFIG


def get_logger(name: str = "mfu_chat_desktop") -> logging.Logger:
    os.makedirs(CONFIG.config_dir, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(os.path.join(CONFIG.config_dir, "desktop.log"), encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(file_handler)
    return logger
