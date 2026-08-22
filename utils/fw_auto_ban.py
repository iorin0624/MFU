from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping


_CRITICAL_PATTERNS = (
    re.compile(r"(?:^|/)(?:\.env(?:[./_-]|$)|\.git(?:/|$)|\.aws(?:/|$)|\.gcloud(?:/|$)|\.kube(?:/|$))", re.I),
    re.compile(r"(?:^|/)(?:wp-config(?:\.[^/]*)?|google-services\.json|service[-_]?account(?:key)?\.json|client[-_]?secret(?:s)?\.json)(?:$|[/?])", re.I),
    re.compile(r"(?:^|/)(?:actuator|vendor/phpunit|alfacgiapi|cgi-bin)(?:/|$)", re.I),
    re.compile(r"(?:\.\./|%2e%2e|%252e%252e|/etc/passwd|/proc/self/environ)", re.I),
)

_SUSPICIOUS_PATTERNS = (
    re.compile(r"(?:^|/)(?:wp-admin|wp-content|wp-includes)(?:/|$)", re.I),
    re.compile(r"(?:^|/)[^/?]+\.php(?:[/?]|$)", re.I),
    re.compile(r"(?:^|/)(?:credentials?|secrets?)(?:\.[^/?]+|/|$)", re.I),
    re.compile(r"(?:^|/)(?:docker-compose|application|bootstrap|database)\.(?:ya?ml|json|ini|conf)(?:[/?]|$)", re.I),
)


@dataclass(frozen=True)
class BanEvidence:
    reason: str
    count: int
    distinct_paths: int
    sample_paths: tuple[str, ...]


@dataclass(frozen=True)
class BanEscalation:
    offense_number: int
    action_kind: str
    duration_sec: int | None


def choose_ban_escalation(
    *,
    prior_count: int,
    escalation_class: str,
    settings: Mapping[str, object],
) -> BanEscalation:
    """Choose the temporary/permanent action for a successful prior-ban count."""
    offense_number = max(0, int(prior_count)) + 1
    category = "sensitive" if escalation_class == "sensitive" else "generic"
    permanent_threshold = int(
        settings["sensitive_permanent_threshold"]
        if category == "sensitive"
        else settings["generic_permanent_threshold"]
    )
    if offense_number >= permanent_threshold:
        return BanEscalation(offense_number, "permanent", None)
    if category == "generic" and offense_number >= 3:
        return BanEscalation(
            offense_number,
            "temporary",
            int(settings["generic_third_ban_duration_sec"]),
        )
    if offense_number >= 2:
        return BanEscalation(
            offense_number,
            "temporary",
            int(settings["repeat_ban_duration_sec"]),
        )
    return BanEscalation(
        offense_number,
        "temporary",
        int(settings["ban_duration_sec"]),
    )


def classify_request_path(path: str, *, endpoint: str = "", status: int = 404) -> str:
    """Classify a failed request without treating normal MFU 404s as attacks."""
    value = str(path or "").strip()
    if not value:
        return "ignore"

    # Attack signatures always win, even if a Flask route happened to match.
    if any(pattern.search(value) for pattern in _CRITICAL_PATTERNS):
        return "critical"
    if any(pattern.search(value) for pattern in _SUSPICIOUS_PATTERNS):
        return "suspicious"

    # An endpoint means Flask deliberately handled the path and returned an
    # application-level error (expired job, deleted image, missing UUID, etc.).
    if str(endpoint or "").strip():
        return "application"

    if int(status or 0) != 404:
        return "ignore"
    if value.startswith(("/static/", "/favicon", "/robots.txt")):
        return "application"
    return "generic"


def evaluate_events(
    rows: Iterable[Mapping[str, object]],
    settings: Mapping[str, object],
    *,
    now: datetime | None = None,
) -> BanEvidence | None:
    """Evaluate centrally fetched request rows using risk-aware thresholds."""
    current = now or datetime.now()
    sensitive_window = float(settings["sensitive_window_sec"])
    sensitive_threshold = int(settings["sensitive_threshold"])
    short_window = float(settings["short_window_sec"])
    short_threshold = int(settings["short_threshold"])
    cumulative_window = float(settings["ip_window_sec"])
    cumulative_threshold = int(settings["ip_threshold"])

    sensitive: list[tuple[datetime, str]] = []
    generic: list[tuple[datetime, str]] = []
    for row in rows:
        occurred_at = row.get("log_date")
        if not isinstance(occurred_at, datetime):
            continue
        path = str(row.get("path") or "")
        category = classify_request_path(
            path,
            endpoint=str(row.get("endpoint") or ""),
            status=int(row.get("status") or 0),
        )
        age = max(0.0, (current - occurred_at).total_seconds())
        if category in {"critical", "suspicious"} and age <= sensitive_window:
            sensitive.append((occurred_at, path))
        elif category == "generic" and age <= cumulative_window:
            generic.append((occurred_at, path))

    if len(sensitive) >= sensitive_threshold:
        unique = tuple(dict.fromkeys(path for _, path in sensitive))
        return BanEvidence("sensitive", len(sensitive), len(unique), unique[:5])

    short_paths = {
        path for occurred_at, path in generic
        if max(0.0, (current - occurred_at).total_seconds()) <= short_window
    }
    if len(short_paths) >= short_threshold:
        return BanEvidence("short", len(short_paths), len(short_paths), tuple(sorted(short_paths))[:5])

    cumulative_paths = {path for _, path in generic}
    if len(cumulative_paths) >= cumulative_threshold:
        return BanEvidence(
            "cumulative",
            len(cumulative_paths),
            len(cumulative_paths),
            tuple(sorted(cumulative_paths))[:5],
        )
    return None


def enforcement_enabled(settings: Mapping[str, object], *, now: datetime | None = None) -> bool:
    if str(settings.get("mode") or "observe").lower() == "enforce":
        return True
    until = str(settings.get("observe_until") or "").strip()
    if not until:
        return False
    try:
        parsed = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            current = now or datetime.now().astimezone()
            if current.tzinfo is None:
                current = current.astimezone()
            return current >= parsed
        current = now or datetime.now()
        if current.tzinfo is not None:
            current = current.astimezone().replace(tzinfo=None)
        return current >= parsed
    except ValueError:
        return False
