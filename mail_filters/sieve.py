from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any


MAX_RULES = 200
MAX_CONDITIONS = 20
MAX_ACTIONS = 20
MAX_TEXT_LENGTH = 2000

_RULE_START_RE = re.compile(r"(?m)^#\s*rule:\s*(.*?)\s*$")
_REQUIRE_RE = re.compile(r"(?mis)^\s*require\s+\[(.*?)\]\s*;")
_QUOTED_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SieveRuleError(ValueError):
    pass


def script_hash(script: str) -> str:
    return hashlib.sha256(script.encode("utf-8")).hexdigest()


def normalize_text(value: Any, *, max_length: int = MAX_TEXT_LENGTH) -> str:
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    if _CONTROL_RE.search(text):
        raise SieveRuleError("制御文字は使用できません")
    if len(text) > max_length:
        raise SieveRuleError(f"文字数が上限（{max_length}文字）を超えています")
    return text


def quote_sieve(value: Any) -> str:
    text = normalize_text(value)
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\r", "").replace("\n", "\\n")
    return f'"{text}"'


def _unquote(value: str) -> str:
    return unicodedata.normalize(
        "NFC",
        re.sub(r"\\(.)", lambda m: "\n" if m.group(1) == "n" else m.group(1), value),
    )


def _parse_string_arg(text: str) -> tuple[list[str], str] | None:
    value = text.lstrip()
    if not value:
        return None
    if value.startswith("["):
        in_quote = False
        escaped = False
        end = None
        for index, char in enumerate(value[1:], start=1):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_quote = not in_quote
                continue
            if char == "]" and not in_quote:
                end = index
                break
        if end is None:
            return None
        values = [_unquote(item) for item in _QUOTED_RE.findall(value[1:end])]
        return values, value[end + 1 :].strip()
    match = _QUOTED_RE.match(value)
    if not match:
        return None
    return [_unquote(match.group(1))], value[match.end() :].strip()


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    in_quote = False
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == '"':
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if char in "([":
            depth += 1
        elif char in ")]":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_condition(text: str) -> dict[str, Any] | None:
    item = " ".join(text.strip().split())
    match = re.match(
        r"^(not\s+)?(header|address|envelope|body)\s+:(contains|is|matches|regex)\s+(.+)$",
        item,
        re.I,
    )
    if not match:
        return None
    first = _parse_string_arg(match.group(4))
    if not first:
        return None
    if match.group(2).lower() == "body":
        values, remainder = first
        if remainder or not values:
            return None
        return {
            "type": "body",
            "operator": match.group(3).lower(),
            "negated": bool(match.group(1)),
            "fields": [],
            "values": values,
        }
    fields, remainder = first
    second = _parse_string_arg(remainder)
    if not second or second[1]:
        return None
    values = second[0]
    if not fields or not values:
        return None
    return {
        "type": match.group(2).lower(),
        "operator": match.group(3).lower(),
        "negated": bool(match.group(1)),
        "fields": fields,
        "values": values,
    }


def _parse_test_expression(text: str) -> tuple[str, list[dict[str, Any]]] | None:
    expression = " ".join(text.strip().split())
    if expression.lower() == "true":
        return "all", [{"type": "true", "operator": "is", "fields": [], "values": []}]
    compound = re.match(r"^(allof|anyof)\s*\((.*)\)$", expression, re.I | re.S)
    mode = "all"
    condition_texts = [expression]
    if compound:
        mode = "all" if compound.group(1).lower() == "allof" else "any"
        condition_texts = _split_top_level(compound.group(2))
    conditions: list[dict[str, Any]] = []
    for condition_text in condition_texts:
        condition = _parse_condition(condition_text)
        if not condition:
            return None
        conditions.append(condition)
    return mode, conditions


def _parse_action(statement: str) -> dict[str, Any] | None:
    text = " ".join(statement.strip().split())
    lower = text.lower()
    if lower in {"keep", "stop", "discard"}:
        return {"type": lower, "value": "", "copy": False}
    match = re.match(r"^(fileinto|redirect)\s+(:copy\s+)?(.+)$", text, re.I)
    if match:
        parsed = _parse_string_arg(match.group(3))
        if not parsed or parsed[1] or len(parsed[0]) != 1:
            return None
        return {
            "type": match.group(1).lower(),
            "value": parsed[0][0],
            "copy": bool(match.group(2)),
        }
    match = re.match(r"^(setflag|addflag|reject)\s+(.+)$", text, re.I)
    if match:
        parsed = _parse_string_arg(match.group(2))
        if not parsed or parsed[1] or len(parsed[0]) != 1:
            return None
        return {
            "type": match.group(1).lower(),
            "value": parsed[0][0],
            "copy": False,
        }
    return None


def _parse_rule_block(name: str, block: str) -> dict[str, Any]:
    original = f"# rule:{name}\n{block}".rstrip() + "\n"
    match = re.match(r"(?is)^\s*if\s+(.+?)\s*\{\s*(.*?)\s*\}\s*$", block.strip())
    if not match:
        return {"name": name, "enabled": True, "raw": original}
    parsed_test = _parse_test_expression(match.group(1))
    if not parsed_test:
        return {"name": name, "enabled": True, "raw": original}
    statements = [item.strip() for item in match.group(2).split(";") if item.strip()]
    actions: list[dict[str, Any]] = []
    for statement in statements:
        action = _parse_action(statement)
        if not action:
            return {"name": name, "enabled": True, "raw": original}
        actions.append(action)
    if not actions:
        return {"name": name, "enabled": True, "raw": original}
    return {
        "name": name,
        "enabled": True,
        "mode": parsed_test[0],
        "conditions": parsed_test[1],
        "actions": actions,
        "raw": None,
    }


def parse_script(script: str) -> dict[str, Any]:
    normalized = script.replace("\r\n", "\n").replace("\r", "\n")
    require_match = _REQUIRE_RE.search(normalized)
    requirements = (
        [_unquote(item) for item in _QUOTED_RE.findall(require_match.group(1))]
        if require_match
        else []
    )
    starts = list(_RULE_START_RE.finditer(normalized))
    rules: list[dict[str, Any]] = []
    for index, start in enumerate(starts[:MAX_RULES]):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(normalized)
        name = normalize_text(start.group(1), max_length=200) or f"ルール{index + 1}"
        body = normalized[start.end() : end]
        rules.append(_parse_rule_block(name, body))
    preamble_end = starts[0].start() if starts else len(normalized)
    preamble = normalized[:preamble_end]
    non_require_preamble = _REQUIRE_RE.sub("", preamble).strip()
    return {
        "requirements": sorted(set(requirements)),
        "rules": rules,
        "preamble": non_require_preamble,
        "unparsed_script": normalized if not starts and normalized.strip() else "",
    }


def _format_arg(values: list[str]) -> str:
    if len(values) == 1:
        return quote_sieve(values[0])
    return "[" + ",".join(quote_sieve(value) for value in values) + "]"


def _condition_to_sieve(condition: dict[str, Any]) -> str:
    condition_type = str(condition.get("type") or "").lower()
    if condition_type == "true":
        return "true"
    if condition_type not in {"header", "address", "envelope", "body"}:
        raise SieveRuleError("未対応の条件種類です")
    operator = str(condition.get("operator") or "contains").lower()
    if operator not in {"contains", "is", "matches", "regex", "starts", "ends"}:
        raise SieveRuleError("未対応の一致方法です")
    values = [normalize_text(value) for value in condition.get("values") or []]
    values = [value for value in values if value]
    sieve_operator = operator
    if operator in {"starts", "ends"}:
        values = [
            ("^" + re.escape(value)) if operator == "starts" else (re.escape(value) + "$")
            for value in values
        ]
        sieve_operator = "regex"
    prefix = "not " if bool(condition.get("negated")) else ""
    if condition_type == "body":
        if not values:
            raise SieveRuleError("検索値を入力してください")
        return f"{prefix}body :{sieve_operator} {_format_arg(values)}"
    fields = [normalize_text(value, max_length=200) for value in condition.get("fields") or []]
    fields = [value for value in fields if value]
    if not fields or not values:
        raise SieveRuleError("条件の対象と検索値を入力してください")
    return f"{prefix}{condition_type} :{sieve_operator} {_format_arg(fields)} {_format_arg(values)}"


def _action_to_sieve(action: dict[str, Any]) -> str:
    action_type = str(action.get("type") or "").lower()
    if action_type in {"keep", "stop", "discard"}:
        return action_type
    if action_type in {"fileinto", "redirect"}:
        value = normalize_text(action.get("value"))
        if not value:
            raise SieveRuleError("振り分け先または転送先を入力してください")
        copy_tag = " :copy" if bool(action.get("copy")) else ""
        return f"{action_type}{copy_tag} {quote_sieve(value)}"
    if action_type in {"setflag", "addflag", "reject"}:
        value = normalize_text(action.get("value"))
        if not value:
            raise SieveRuleError("アクションの値を入力してください")
        return f"{action_type} {quote_sieve(value)}"
    raise SieveRuleError("未対応のアクションです")


def validate_rules_document(document: dict[str, Any]) -> dict[str, Any]:
    rules = document.get("rules")
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        raise SieveRuleError("ルール数が不正です")
    normalized_rules: list[dict[str, Any]] = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise SieveRuleError("ルール形式が不正です")
        name = normalize_text(rule.get("name"), max_length=200) or f"ルール{index + 1}"
        enabled = bool(rule.get("enabled", True))
        raw = rule.get("raw")
        if raw:
            raw_text = str(raw).replace("\r\n", "\n").replace("\r", "\n")
            if len(raw_text) > 100_000:
                raise SieveRuleError("高度なルールが大きすぎます")
            normalized_rules.append({"name": name, "enabled": enabled, "raw": raw_text})
            continue
        mode = str(rule.get("mode") or "all").lower()
        if mode not in {"all", "any"}:
            raise SieveRuleError("条件結合方法が不正です")
        conditions = rule.get("conditions") or []
        actions = rule.get("actions") or []
        if not isinstance(conditions, list) or not 1 <= len(conditions) <= MAX_CONDITIONS:
            raise SieveRuleError("条件数が不正です")
        if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
            raise SieveRuleError("アクション数が不正です")
        normalized_rules.append(
            {
                "name": name,
                "enabled": enabled,
                "mode": mode,
                "conditions": conditions,
                "actions": actions,
                "raw": None,
            }
        )
    requirements = [
        normalize_text(value, max_length=100)
        for value in document.get("requirements") or []
        if normalize_text(value, max_length=100)
    ]
    return {
        "requirements": sorted(set(requirements)),
        "rules": normalized_rules,
        "preamble": str(document.get("preamble") or "").strip(),
        "unparsed_script": str(document.get("unparsed_script") or ""),
    }


def generate_script(document: dict[str, Any]) -> str:
    doc = validate_rules_document(document)
    unparsed_script = str(doc.get("unparsed_script") or "")
    if unparsed_script:
        if doc["rules"]:
            raise SieveRuleError(
                "形式を判別できない既存Sieveと画面編集ルールは同時に保存できません"
            )
        if re.search(
            r"(?i)\bvnd\.dovecot\.execute\b|\bexecute\s*(?:;|:|\")",
            unparsed_script,
        ):
            raise SieveRuleError("外部コマンド実行を含むルールは利用できません")
        normalized = unparsed_script.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.rstrip() + "\n"
    requirements = set(doc["requirements"])
    blocks: list[str] = []
    for rule in doc["rules"]:
        if not rule["enabled"]:
            continue
        if rule.get("raw"):
            raw = str(rule["raw"]).strip()
            if re.search(r"(?i)\bvnd\.dovecot\.execute\b|\bexecute\s*(?:;|:|\")", raw):
                raise SieveRuleError("外部コマンド実行を含むルールは利用できません")
            blocks.append(raw)
            continue
        conditions = rule["conditions"]
        condition_parts: list[str] = []
        for condition_index, item in enumerate(conditions, start=1):
            try:
                condition_parts.append(_condition_to_sieve(item))
            except SieveRuleError as exc:
                raise SieveRuleError(
                    f"ルール「{rule['name']}」の条件{condition_index}: {exc}"
                ) from exc
        if len(condition_parts) == 1:
            expression = condition_parts[0]
        else:
            function = "allof" if rule["mode"] == "all" else "anyof"
            expression = f"{function} ({', '.join(condition_parts)})"
        action_lines: list[str] = []
        for action_index, action in enumerate(rule["actions"], start=1):
            try:
                action_text = _action_to_sieve(action)
            except SieveRuleError as exc:
                raise SieveRuleError(
                    f"ルール「{rule['name']}」の処理{action_index}: {exc}"
                ) from exc
            action_lines.append(f"\t{action_text};")
            action_type = str(action.get("type") or "").lower()
            if action_type == "fileinto":
                requirements.add("fileinto")
            if action_type in {"setflag", "addflag"}:
                requirements.add("imap4flags")
            if action_type in {"fileinto", "redirect"} and bool(action.get("copy")):
                requirements.add("copy")
            if action_type == "reject":
                requirements.add("reject")
        for condition in conditions:
            condition_type = str(condition.get("type") or "").lower()
            operator = str(condition.get("operator") or "").lower()
            if condition_type == "body":
                requirements.add("body")
            if condition_type == "envelope":
                requirements.add("envelope")
            if operator in {"regex", "starts", "ends"}:
                requirements.add("regex")
        blocks.append(
            "\n".join(
                [
                    f"# rule:{normalize_text(rule['name'], max_length=200)}",
                    f"if {expression}",
                    "{",
                    *action_lines,
                    "}",
                ]
            )
        )
    require_line = ""
    if requirements:
        require_line = "require [" + ",".join(quote_sieve(item) for item in sorted(requirements)) + "];"
    preamble_parts = [part for part in (require_line, doc["preamble"]) if part]
    output_parts = preamble_parts + blocks
    if not output_parts:
        return ""
    return "\n".join(output_parts).rstrip() + "\n"
