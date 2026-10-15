"""Format X follow deltas for MFU notifications without storing full lists."""

from __future__ import annotations

import re


_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}\Z")
_SECTIONS = (
    ("followers", "added", "フォロワー増加"),
    ("followers", "removed", "フォロワー減少"),
    ("following", "added", "フォロー中の追加"),
    ("following", "removed", "フォロー中の解除"),
)


def _handles(rows: object) -> list[str]:
    if not isinstance(rows, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        handle = str(row.get("handle") or "").strip()
        key = handle.lower()
        if not _HANDLE.fullmatch(handle) or key in seen:
            continue
        seen.add(key)
        result.append(handle)
    return result


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2_000_000 else None


def build_notification(
    status: str, counts: dict, by_type: dict
) -> tuple[str, list[dict[str, object]]]:
    """Return a short preview and complete, clickable changed-handle fields.

    Only validated @handles are stored in MFU's notification history; account
    snapshots and unchanged accounts remain exclusively on Windows/Chrome.
    """
    followers = _count(counts.get("followers"))
    following = _count(counts.get("following"))
    preview = [
        f"フォロワー {followers if followers is not None else '—'}件、"
        f"フォロー中 {following if following is not None else '—'}件。"
    ]
    fields: list[dict[str, object]] = []
    if status == "success":
        has_overflow = False
        for account_type, change, label in _SECTIONS:
            diff = by_type.get(account_type)
            if not isinstance(diff, dict):
                continue
            handles = _handles(diff.get(change))
            reported = _count(diff.get("addedCount" if change == "added" else "removedCount"))
            total = reported if reported is not None else len(handles)
            preview.append(f"{label} {total}件")
            if not handles:
                continue
            shown = "、".join(f"@{handle}" for handle in handles[:5])
            remainder = f"、ほか{len(handles) - 5}件" if len(handles) > 5 else ""
            has_overflow |= len(handles) > 5
            preview[-1] += f"（{shown}{remainder}）"
            fields.append({
                "name": f"{label}（{total}件）",
                "value": "\n".join(
                    f"[@{handle}](https://x.com/{handle})" for handle in handles
                ),
                "inline": False,
            })
        if not fields:
            preview.append("増減はありません。")
        elif has_overflow:
            preview.append("全件のリンクは通知詳細に表示します。")
    elif status == "incomplete":
        preview.append("取得漏れの可能性があるため、減少判定は行っていません。")
    else:
        preview.append("取得に失敗しました。")
    # Web Push stores a 1,000-character preview. Keep it well below that limit
    # so a profile ID is never cut in the middle.
    description = " ".join(preview)
    if len(description) > 900:
        description = preview[0] + " 増減したIDとリンクは通知詳細に表示します。"
    return description, fields
