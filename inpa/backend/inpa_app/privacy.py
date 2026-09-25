"""Profile-level field visibility policy shared by share and calendar views."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from sqlalchemy import text

AUDIENCES = ("link", "logged_in", "mutual", "private")
PUBLIC_FIELDS = ("date", "park", "costume", "memo")
_COLUMN_BY_FIELD = {
    "date": "show_date",
    "park": "show_park",
    "costume": "show_costume",
    "memo": "show_memo",
}


def empty_matrix() -> dict[str, dict[str, bool]]:
    return {audience: {field: False for field in PUBLIC_FIELDS} for audience in AUDIENCES}


def recommended_matrix() -> dict[str, dict[str, bool]]:
    """Return the privacy defaults shown during first-time setup."""
    matrix = empty_matrix()
    for field in ("date", "park"):
        matrix["link"][field] = True
    for field in ("date", "park", "costume"):
        matrix["logged_in"][field] = True
    for audience in ("mutual", "private"):
        for field in PUBLIC_FIELDS:
            matrix[audience][field] = True
    return matrix


def matrix_from_rows(rows: Iterable[Mapping[str, object]]) -> dict[str, dict[str, bool]]:
    matrix = empty_matrix()
    for row in rows:
        audience = str(row["audience"])
        if audience not in matrix:
            continue
        for field, column in _COLUMN_BY_FIELD.items():
            matrix[audience][field] = bool(row[column])
    return matrix


def validate_matrix(value: object) -> dict[str, dict[str, bool]]:
    if not isinstance(value, dict) or set(value) != set(AUDIENCES):
        raise ValueError("4つの公開範囲をすべて指定してください。")
    result = empty_matrix()
    for audience in AUDIENCES:
        fields = value[audience]
        if not isinstance(fields, dict) or set(fields) != set(PUBLIC_FIELDS):
            raise ValueError("公開情報の項目を確認してください。")
        for field in PUBLIC_FIELDS:
            if not isinstance(fields[field], bool):
                raise TypeError("公開情報はチェック状態で指定してください。")
            result[audience][field] = fields[field]
    return result


def effective_fields(matrix: Mapping[str, Mapping[str, bool]], audience: str | None) -> set[str]:
    """Return cumulative fields visible at the viewer's trust level."""
    if audience not in AUDIENCES:
        return set()
    allowed: set[str] = set()
    for level in AUDIENCES[: AUDIENCES.index(audience) + 1]:
        allowed.update(field for field in PUBLIC_FIELDS if matrix[level].get(field) is True)
    return allowed


def viewer_audience(
    *, owner: bool, logged_in: bool, owner_follows_viewer: bool,
    viewer_follows_owner: bool, blocked: bool,
) -> str | None:
    if owner:
        return "private"
    if blocked:
        return None
    if logged_in and owner_follows_viewer and viewer_follows_owner:
        return "mutual"
    if logged_in:
        return "logged_in"
    return "link"


def load_matrix(connection, user_id: int, season_id: int | None = None) -> dict[str, dict[str, bool]]:
    if season_id is not None:
        rows = connection.execute(
            text(
                "SELECT audience,show_date,show_park,show_costume,show_memo "
                "FROM user_season_privacy_settings "
                "WHERE user_id=:user_id AND season_id=:season_id"
            ),
            {"user_id": user_id, "season_id": season_id},
        ).mappings().all()
        if len(rows) == len(AUDIENCES):
            return matrix_from_rows(rows)
    rows = connection.execute(
        text(
            "SELECT audience,show_date,show_park,show_costume,show_memo "
            "FROM user_privacy_settings WHERE user_id=:user_id"
        ),
        {"user_id": user_id},
    ).mappings().all()
    return matrix_from_rows(rows)


def save_matrix(
    connection, user_id: int, matrix: Mapping[str, Mapping[str, bool]],
    season_id: int | None = None,
) -> None:
    table = "user_season_privacy_settings" if season_id is not None else "user_privacy_settings"
    key_columns = "user_id,season_id,audience" if season_id is not None else "user_id,audience"
    key_values = ":user_id,:season_id,:audience" if season_id is not None else ":user_id,:audience"
    for audience in AUDIENCES:
        values = {column: int(matrix[audience][field]) for field, column in _COLUMN_BY_FIELD.items()}
        connection.execute(
            text(
                f"INSERT INTO {table} "
                f"({key_columns},show_date,show_park,show_costume,show_memo) "
                f"VALUES ({key_values},:show_date,:show_park,:show_costume,:show_memo) "
                "ON DUPLICATE KEY UPDATE show_date=VALUES(show_date),show_park=VALUES(show_park),"
                "show_costume=VALUES(show_costume),show_memo=VALUES(show_memo),"
                "updated_at=UTC_TIMESTAMP(6)"
            ),
            {"user_id": user_id, "season_id": season_id, "audience": audience, **values},
        )


def create_default_matrix(connection, user_id: int) -> None:
    save_matrix(connection, user_id, recommended_matrix())
