from __future__ import annotations


MAX_LOG_DESTINATION_LENGTH = 120


def build_fare_search_marker(destination: object, *, succeeded: bool) -> str:
    """Build a single-line access-log marker for an executed fare search."""
    raw_destination = "" if destination is None else str(destination)
    normalized_destination = "".join(
        " " if ord(character) < 32 or ord(character) == 127 else character
        for character in raw_destination
    )[:MAX_LOG_DESTINATION_LENGTH]
    result_label = "成功" if succeeded else "失敗"
    return (
        "[FARE_ESTIMATE_SEARCH] "
        f"結果：{result_label} 到着地点：{normalized_destination}"
    )
