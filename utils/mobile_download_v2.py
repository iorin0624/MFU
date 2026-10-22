"""Pure state rules for resumable shortcut downloads."""

from __future__ import annotations


FILE_STATES = ("pending", "saving", "saved", "failed", "uncertain")


def file_transition_error(previous: str, requested: str) -> str | None:
    if requested not in {"saving", "saved", "failed"}:
        return "invalid_file_state"
    if previous == "saved":
        return None if requested == "saved" else "already_saved"
    if previous == "uncertain":
        return "uncertain_requires_resume"
    if requested == "saved" and previous != "saving":
        return "saving_report_required"
    if requested == "saving" and previous not in {"pending", "failed", "saving"}:
        return "invalid_file_transition"
    return None


def resume_file_ids(files: list[dict], retry_uncertain: set[str]) -> tuple[set[str], str | None]:
    uncertain = {row["file_id"] for row in files if row["state"] == "uncertain"}
    if not retry_uncertain.issubset(uncertain):
        return set(), "invalid_retry_uncertain"
    retry = {row["file_id"] for row in files
             if row["state"] in {"pending", "failed"}} | retry_uncertain
    if not retry:
        return set(), "uncertain_files_require_confirmation"
    return retry, None
