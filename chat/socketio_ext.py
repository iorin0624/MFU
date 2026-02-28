# /mnt/mfu/app/chat/socketio_ext.py
from __future__ import annotations

import os

from flask_socketio import SocketIO

# Socket.IO 設定
#
# - gunicorn を複数 worker で動かす場合、worker 間でイベントを中継するために
#   message_queue（Redis）を必ず設定する。
# - Redis が無い状態でもアプリを落とさないため、環境変数が無い場合は
#   message_queue を設定しない（= worker=1 前提の動作）。
#
# 環境変数例:
#   CHAT_SOCKETIO_MESSAGE_QUEUE=redis://127.0.0.1:6379/0
#   CHAT_SOCKETIO_CORS=*
#
MESSAGE_QUEUE = os.environ.get("CHAT_SOCKETIO_MESSAGE_QUEUE", "").strip()
CORS = os.environ.get("CHAT_SOCKETIO_CORS", "*").strip()

kwargs: dict = {
    "async_mode": "threading",
    "cors_allowed_origins": CORS,
}

# Redis を使う場合のみ message_queue を有効化
if MESSAGE_QUEUE:
    kwargs["message_queue"] = MESSAGE_QUEUE

socketio = SocketIO(**kwargs)

# message_queue 設定後の直後あたりに追加
import logging
logging.getLogger(__name__).warning("chat socketio message_queue=%s", kwargs.get("message_queue"))