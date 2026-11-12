from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

_URL_PATTERN = re.compile(r'(https?://[^\s<>"\')\]]+)')
_QA_HEADER_PATTERN = re.compile(r'^Q([1-9][0-9]*)[.．]\s*(\S.*)$')
_QA_PREFIX_PATTERN = re.compile(r'^Q[0-9]+')
_QA_CATEGORY_PREFIX = '【カテゴリ】'
_QA_SUMMARY_PREFIX = '【要点】'
PROFILE_QA_CATEGORIES = (
    '依頼・日程', '費用・支払い', 'キャンセル・変更', '納品・保存', '写真の利用', '事業者情報', 'その他',
)
MAX_QA_CATEGORY_LENGTH = 30
MAX_QA_SUMMARY_LENGTH = 200

DEFAULT_PROFILE_QA_TEXT = (
    "Q1. 撮影の依頼はどのようにすればよいですか？\n"
    "まずは予定カレンダーで空き状況をご確認いただき、希望日時・撮影場所・撮影内容を添えてXorInstagramのDMまでご連絡ください。内容を確認のうえ、お返事いたします。\n"
    "\n"
    "Q2. カレンダーで空いていれば、必ず撮影してもらえますか？\n"
    "申し訳ありませんが、空いていても前後の予定や移動時間によってはお断りする場合があります。また、カレンダー上部に「この月の撮影予定の追加は行っていません。」と表示されている場合は、新規のご依頼を受け付けておりません。"
)
MAX_PROFILE_QA_LENGTH = 30000
_JST = timezone(timedelta(hours=9))


def normalize_plain_text_for_display(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = value
    normalized = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", normalized)
    normalized = re.sub(r"(?i)&lt;\s*br\s*/?\s*&gt;", "\n", normalized)
    normalized = re.sub(r"(?i)&amp;lt;\s*br\s*/?\s*&amp;gt;", "\n", normalized)

    return normalized.replace("\r\n", "\n").replace("\r", "\n")


def linkify_plain_text_for_display(value: str | None) -> str | None:
    normalized = normalize_plain_text_for_display(value)
    if normalized is None:
        return None

    escaped = html.escape(normalized)

    def repl(match: re.Match[str]) -> str:
        url = match.group(1)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer nofollow">{url}</a>'

    return _URL_PATTERN.sub(repl, escaped)


def _legacy_qa_category(question: str) -> str:
    """Offer useful categories to existing Q&A without rewriting its stored text."""
    if '特定商取引' in question or '事業者' in question:
        return '事業者情報'
    if any(word in question for word in ('キャンセル', '雨天', '悪天候', '日程変更')):
        return 'キャンセル・変更'
    if any(word in question for word in ('納品', '受け取', '再納品', '保存期間')):
        return '納品・保存'
    if any(word in question for word in ('商用利用', '写真集', '雑誌', '掲載', 'SNS', '加工', 'レタッチ', '著作権')):
        return '写真の利用'
    if any(word in question for word in ('費用', '交通費', '支払', 'スタジオの予約', 'スタジオ代')):
        return '費用・支払い'
    if any(word in question for word in ('撮影', '依頼', 'カレンダー', '仮押さえ', '地域', 'コスプレ')):
        return '依頼・日程'
    return 'その他'


def parse_profile_qa_text(value: str | None) -> list[dict[str, str | int]]:
    """Split Q&A text and optional per-question category/summary metadata."""
    text = normalize_plain_text_for_display(value) or ""
    if len(text) > MAX_PROFILE_QA_LENGTH:
        raise ValueError(f"Q&Aは{MAX_PROFILE_QA_LENGTH}文字以内で入力してください。")
    if not text.strip():
        return []

    items: list[dict[str, str | int]] = []
    current: dict[str, str | int] | None = None
    answer_lines: list[str] = []
    answer_started = False

    def finish_current() -> None:
        if current is None:
            return
        answer = "\n".join(answer_lines).strip()
        if not answer:
            raise ValueError(f"Q{current['number']}の回答を入力してください。")
        current["answer"] = answer
        items.append(current)

    for line_number, line in enumerate(text.split("\n"), start=1):
        stripped = line.strip()
        match = _QA_HEADER_PATTERN.fullmatch(stripped)
        if match:
            finish_current()
            number = int(match.group(1))
            expected = len(items) + 1
            if number != expected:
                raise ValueError(f"{line_number}行目: Q{expected}. から順番に番号を付けてください。")
            question = match.group(2).strip()
            current = {
                "number": number, "question": question, "answer": "",
                "category": _legacy_qa_category(question), "summary": "",
            }
            answer_lines = []
            answer_started = False
        elif _QA_PREFIX_PATTERN.match(stripped):
            raise ValueError(f"{line_number}行目: 質問は「Q1. 質問文」の形式で入力してください。")
        elif current is None:
            if stripped:
                raise ValueError(f"{line_number}行目: 最初の質問は「Q1. 質問文」から始めてください。")
        elif stripped.startswith(_QA_CATEGORY_PREFIX) or stripped.startswith(_QA_SUMMARY_PREFIX):
            if answer_started:
                raise ValueError(f"{line_number}行目: カテゴリと要点は回答本文より前に入力してください。")
            if stripped.startswith(_QA_CATEGORY_PREFIX):
                category = stripped[len(_QA_CATEGORY_PREFIX):].strip()
                if not category or len(category) > MAX_QA_CATEGORY_LENGTH:
                    raise ValueError(f"{line_number}行目: カテゴリは1～{MAX_QA_CATEGORY_LENGTH}文字で入力してください。")
                if current.get('category_explicit'):
                    raise ValueError(f"{line_number}行目: カテゴリは1問につき1行までです。")
                current['category'] = category
                current['category_explicit'] = 1
            else:
                summary = stripped[len(_QA_SUMMARY_PREFIX):].strip()
                if not summary or len(summary) > MAX_QA_SUMMARY_LENGTH:
                    raise ValueError(f"{line_number}行目: 要点は1～{MAX_QA_SUMMARY_LENGTH}文字で入力してください。")
                if current.get('summary'):
                    raise ValueError(f"{line_number}行目: 要点は1問につき1行までです。")
                current['summary'] = summary
        else:
            answer_lines.append(line)
            if stripped:
                answer_started = True

    finish_current()
    for item in items:
        item.pop('category_explicit', None)
    return items


def list_profile_qa_categories(items: list[dict[str, str | int]]) -> list[dict[str, str | int]]:
    counts: dict[str, int] = {}
    for item in items:
        category = str(item['category'])
        counts[category] = counts.get(category, 0) + 1
    names = [name for name in PROFILE_QA_CATEGORIES if name in counts]
    names.extend(name for name in counts if name not in PROFILE_QA_CATEGORIES)
    return [{'name': name, 'count': counts[name]} for name in names]


def format_qa_updated_at(value: datetime | None) -> str | None:
    """Format a UTC database timestamp as an unpadded Japanese calendar date."""
    if value is None:
        return None
    utc_value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    local = utc_value.astimezone(_JST)
    return f"{local.year}/{local.month}/{local.day}更新"


def split_legacy_profile_qa(notes: str | None) -> tuple[str | None, str] | None:
    """Extract a trailing Q&A block from the old shooting-request notes."""
    text = normalize_plain_text_for_display(notes) or ""
    marker = re.search(r"(?m)^Q&A[ \t]*\n(?=\s*Q1[.．])", text)
    if marker is None:
        return None
    qa_text = text[marker.end():].strip()
    parse_profile_qa_text(qa_text)
    return text[:marker.start()].rstrip() or None, qa_text
