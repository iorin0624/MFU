# app/external_login_user/jinja.py
from __future__ import annotations
import re
from markupsafe import Markup, escape

def register_jinja(bp):
    @bp.app_template_global()
    def my_role_label(role: str) -> str:
        m = {"none":"—","camera":"カメラマン","assistant":"アシスタント","cosplayer":"衣装"}
        return m.get((role or "none").lower(), "—")

    @bp.app_template_filter("linkify")
    def jinja_linkify(text: str | None):
        if not text:
            return ""
        s = escape(text)
        pattern = re.compile(r'(https?://[^\s<>"\')\]]+)')
        def repl(m: re.Match):
            url = m.group(1)
            return Markup(f'<a href="{url}" target="_blank" rel="noopener nofollow">{url}</a>')
        return Markup(pattern.sub(repl, s))
