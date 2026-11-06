from flask_socketio import SocketIO
from app.utils.socketio_runtime import allowed_origins

socketio = SocketIO(async_mode="threading", cors_allowed_origins=allowed_origins())
