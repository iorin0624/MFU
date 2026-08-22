import os
import threading
import time
import mysql.connector
from mysql.connector import errors, pooling
from config import MYSQL_CONFIG


_pool = None
_pool_pid = None
_pool_lock = threading.Lock()


def _connection_pool():
    """Return the process-local MySQL pool used by this Gunicorn worker."""
    global _pool, _pool_pid
    pid = os.getpid()
    if _pool is not None and _pool_pid == pid:
        return _pool
    with _pool_lock:
        if _pool is None or _pool_pid != pid:
            pool_size = max(2, min(32, int(os.environ.get("MYSQL_POOL_SIZE", "10"))))
            pool_config = dict(MYSQL_CONFIG)
            # A few legacy callers fetch only the first row.  Consume the
            # remainder before returning that TCP connection to the pool.
            pool_config.setdefault("consume_results", True)
            _pool = pooling.MySQLConnectionPool(
                pool_name=f"mfu_{pid}",
                pool_size=pool_size,
                pool_reset_session=True,
                **pool_config,
            )
            _pool_pid = pid
    return _pool

def get_db(retries: int = 3, delay: float = 0.5):
    """
    MySQL接続ラッパ。
    瞬間的なネットワーク不調を吸収するため、ちょっとだけリトライを入れる。
    """
    last_err = None
    for attempt in range(retries):
        try:
            conn = _connection_pool().get_connection()
            if not conn.is_connected():
                conn.reconnect(attempts=1, delay=0)
            try:
                from flask import g, has_app_context
                if has_app_context():
                    tracked = getattr(g, "_mfu_db_connections", None)
                    if tracked is None:
                        tracked = []
                        g._mfu_db_connections = tracked
                    tracked.append(conn)
            except Exception:
                pass
            return conn
        except (errors.OperationalError, errors.DatabaseError, errors.InterfaceError, errors.PoolError) as e:
            # errno 99/2003/2013 と、一時的なプール枯渇を短くリトライする。
            last_err = e
            if attempt == retries - 1:
                raise
            time.sleep(delay)
    raise last_err


def close_tracked_connections() -> None:
    """Return every connection borrowed in the current Flask app context."""
    try:
        from flask import g, has_app_context
        if not has_app_context():
            return
        tracked = list(getattr(g, "_mfu_db_connections", ()) or ())
        g._mfu_db_connections = []
    except Exception:
        return
    for conn in tracked:
        try:
            # PooledMySQLConnection keeps the wrapper after close(), but sets
            # its underlying connection to None.  Closing it twice would ask
            # mysql-connector to create an extra connection in a full pool.
            if getattr(conn, "_cnx", None) is None:
                continue
            conn.close()
        except Exception:
            pass
