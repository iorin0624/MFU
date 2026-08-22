from __future__ import annotations

import html
import re


_CSRF_META_PATTERN = re.compile(
    r'(<meta\s+name="csrf-token"\s+content=")[^"]*(">)',
    flags=re.IGNORECASE,
)


def bind_runtime_csrf_token(html_text: str, token: str) -> str:
    """非同期生成HTMLのCSRFトークンを、現在の閲覧セッションへ差し替える。"""
    escaped_token = html.escape(str(token or ""), quote=True)
    return _CSRF_META_PATTERN.sub(
        lambda match: f"{match.group(1)}{escaped_token}{match.group(2)}",
        html_text,
        count=1,
    )
