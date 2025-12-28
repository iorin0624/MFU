from datetime import datetime
import re

def generate_message(mode, context, username="default"):
    from app.utils.db import get_db

    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT template FROM message_templates
        WHERE username = %s AND mode = %s
    """, (username, mode))
    row = cursor.fetchone()
    db.close()

    if not row:
        return ""

    template = row[0]

    # 日付の整形： 2025-05-06 → 2025年05月06日
    if "date" in context:
        try:
            d = datetime.strptime(context["date"], "%Y-%m-%d")
            context["date"] = d.strftime("%Y年%m月%d日")
        except:
            pass

    # テンプレート内の {{key}} を context で置換
    def repl(m):
        key = m.group(1)
        return context.get(key, f"{{{{{key}}}}}")

    return re.sub(r'{{\s*(\w+)\s*}}', repl, template)
