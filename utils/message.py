from datetime import datetime
import re
import threading


_notification_schema_lock = threading.Lock()
_notification_schema_ready = False


def ensure_notification_message_schema():
    """Create the split notification templates and per-upload snapshots."""
    global _notification_schema_ready
    if _notification_schema_ready:
        return
    with _notification_schema_lock:
        if _notification_schema_ready:
            return
        from app.utils.db import get_db

        db = get_db()
        cursor = db.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_message_templates (
                    username VARCHAR(255) NOT NULL,
                    mode VARCHAR(100) NOT NULL,
                    template TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (username, mode)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                INSERT IGNORE INTO notification_message_templates (username, mode, template)
                SELECT username, mode, template FROM message_templates
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS upload_notification_messages (
                    uuid VARCHAR(64) NOT NULL,
                    mode VARCHAR(255) NOT NULL,
                    message TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    PRIMARY KEY (uuid)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """
            )
            db.commit()
            _notification_schema_ready = True
        finally:
            cursor.close()
            db.close()


def _render_template(table, mode, context, username):
    from app.utils.db import get_db

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        f"SELECT template FROM {table} WHERE username = %s AND mode = %s",
        (username, mode),
    )
    row = cursor.fetchone()
    cursor.close()
    db.close()

    if not row:
        return ""

    values = dict(context)
    if "date" in values:
        try:
            d = datetime.strptime(values["date"], "%Y-%m-%d")
            values["date"] = d.strftime("%Y年%m月%d日")
        except (TypeError, ValueError):
            pass

    def repl(match):
        key = match.group(1)
        return str(values.get(key, f"{{{{{key}}}}}"))

    return re.sub(r'{{\s*(\w+)\s*}}', repl, row[0])

def generate_message(mode, context, username="default"):
    """Render the template shown inside the public viewer."""
    return _render_template("message_templates", mode, context, username)


def generate_notification_message(mode, context, username="default"):
    """Render the template copied or mailed to the recipient."""
    ensure_notification_message_schema()
    return _render_template("notification_message_templates", mode, context, username)
