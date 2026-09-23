"""Authenticated profile, season, and visit APIs."""

from __future__ import annotations

from datetime import date, time, timedelta

from flask import Blueprint, jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .auth.routes import _error, _require_session
from .auth.service import RegistrationError, new_public_id, normalize_social_handle, verify_csrf
from .db import get_engine
from .privacy import load_matrix, save_matrix, validate_matrix

bp = Blueprint("planner", __name__, url_prefix="/api/v1")
PARKS = {"land", "sea", "both", "undecided", None}


def _payload() -> dict[str, object]:
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise RegistrationError("JSON形式で送信してください。")
    return value


def _session(*, csrf: bool = False):
    session, error = _require_session()
    if error:
        return None, error
    if csrf and not verify_csrf(session, request.headers.get("X-CSRF-Token")):
        return None, _error("csrf_failed", "リクエストを確認できませんでした。", 403)
    return session, None


def _profile(row, privacy_matrix) -> dict[str, object]:
    result = dict(row)
    result["x_handle_visible"] = bool(result["x_handle_visible"])
    result["instagram_handle_visible"] = bool(result["instagram_handle_visible"])
    result["privacy_matrix"] = privacy_matrix
    return result


@bp.get("/profile")
def get_profile():
    session, error = _session()
    if error:
        return error
    with get_engine().connect() as connection:
        row = connection.execute(
            text(
                "SELECT public_id, connection_id, display_name, x_handle, instagram_handle, "
                "x_handle_visible, instagram_handle_visible FROM users WHERE id=:id"
            ),
            {"id": session["user_id"]},
        ).mappings().one()
        privacy_matrix = load_matrix(connection, int(session["user_id"]))
    return jsonify(profile=_profile(row, privacy_matrix))


@bp.patch("/profile")
def patch_profile():
    session, error = _session(csrf=True)
    if error:
        return error
    try:
        data = _payload()
        allowed = {
            "display_name", "x_handle", "instagram_handle",
            "x_handle_visible", "instagram_handle_visible",
        }
        if unknown := set(data) - allowed:
            raise RegistrationError(f"更新できない項目が含まれています: {', '.join(sorted(unknown))}")
        updates = dict(data)
        if not updates:
            raise RegistrationError("更新する項目を入力してください。")
        if "display_name" in updates:
            value = updates["display_name"]
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 40:
                raise RegistrationError("表示名は1〜40文字で入力してください。")
            updates["display_name"] = value.strip()
        if "x_handle" in updates:
            updates["x_handle"], updates["x_handle_normalized"] = normalize_social_handle(
                updates["x_handle"], "x"
            )
        if "instagram_handle" in updates:
            updates["instagram_handle"], updates["instagram_handle_normalized"] = normalize_social_handle(
                updates["instagram_handle"], "instagram"
            )
        for key in ("x_handle_visible", "instagram_handle_visible"):
            if key in updates and not isinstance(updates[key], bool):
                raise RegistrationError("表示許可はtrueまたはfalseで指定してください。")
        with get_engine().begin() as connection:
            current = connection.execute(text(
                "SELECT x_handle,instagram_handle FROM users WHERE id=:id FOR UPDATE"
            ), {"id": session["user_id"]}).mappings().one()
            final_x = updates.get("x_handle", current["x_handle"])
            final_instagram = updates.get("instagram_handle", current["instagram_handle"])
            if not final_x and not final_instagram:
                raise RegistrationError("X IDまたはInstagram IDのどちらかは必須です。")
            for column, value, label in (
                ("x_handle_normalized", updates.get("x_handle_normalized"), "X ID"),
                ("instagram_handle_normalized", updates.get("instagram_handle_normalized"), "Instagram ID"),
            ):
                if value and connection.execute(text(
                    f"SELECT 1 FROM users WHERE {column}=:value AND id<>:id LIMIT 1"
                ), {"value": value, "id": session["user_id"]}).first():
                    raise RegistrationError(f"この{label}はすでに登録されています。")
            sets = ", ".join(f"{key}=:{key}" for key in updates)
            connection.execute(
                text(f"UPDATE users SET {sets} WHERE id=:id"),
                {**updates, "id": session["user_id"]},
            )
    except RegistrationError as exc:
        return _error("invalid_request", str(exc), 400)
    except IntegrityError:
        return _error("duplicate_social_id", "このSNS IDはすでに登録されています。", 409)
    return get_profile()


@bp.patch("/privacy-defaults")
def patch_privacy_defaults():
    session, error = _session(csrf=True)
    if error:
        return error
    try:
        data = _payload()
        try:
            matrix = validate_matrix(data.get("privacy_matrix"))
        except (TypeError, ValueError) as exc:
            raise RegistrationError(str(exc)) from exc
        season_public_id = data.get("season_public_id")
        with get_engine().begin() as connection:
            if season_public_id is not None:
                if not isinstance(season_public_id, str) or not season_public_id:
                    raise RegistrationError("シーズンを確認してください。")
                season_id = connection.execute(
                    text("SELECT id FROM seasons WHERE public_id=:id AND is_active=1"),
                    {"id": season_public_id},
                ).scalar()
                if not season_id:
                    raise RegistrationError("シーズンを確認してください。")
                save_matrix(connection, int(session["user_id"]), matrix, int(season_id))
                return jsonify(privacy_matrix=matrix, season_public_id=season_public_id)
            save_matrix(connection, int(session["user_id"]), matrix)
    except RegistrationError as exc:
        return _error("invalid_request", str(exc), 400)
    return get_profile()


@bp.get("/privacy-defaults")
def get_privacy_defaults():
    session, error = _session()
    if error:
        return error
    season_public_id = request.args.get("season_id", "").strip()
    with get_engine().connect() as connection:
        season_id = None
        customized = False
        if season_public_id:
            season_id = connection.execute(
                text("SELECT id FROM seasons WHERE public_id=:id AND is_active=1"),
                {"id": season_public_id},
            ).scalar()
            if not season_id:
                return _error("invalid_season", "シーズンを確認してください。", 400)
            customized = bool(connection.execute(
                text(
                    "SELECT COUNT(*) FROM user_season_privacy_settings "
                    "WHERE user_id=:user_id AND season_id=:season_id"
                ),
                {"user_id": session["user_id"], "season_id": season_id},
            ).scalar())
        matrix = load_matrix(connection, int(session["user_id"]), int(season_id) if season_id else None)
    return jsonify(
        privacy_matrix=matrix, season_public_id=season_public_id or None,
        customized=customized,
    )


@bp.delete("/privacy-defaults/<season_public_id>")
def reset_season_privacy_defaults(season_public_id: str):
    session, error = _session(csrf=True)
    if error:
        return error
    with get_engine().begin() as connection:
        season_id = connection.execute(
            text("SELECT id FROM seasons WHERE public_id=:id AND is_active=1"),
            {"id": season_public_id},
        ).scalar()
        if not season_id:
            return _error("invalid_season", "シーズンを確認してください。", 400)
        connection.execute(
            text(
                "DELETE FROM user_season_privacy_settings "
                "WHERE user_id=:user_id AND season_id=:season_id"
            ),
            {"user_id": session["user_id"], "season_id": season_id},
        )
        matrix = load_matrix(connection, int(session["user_id"]))
    return jsonify(
        privacy_matrix=matrix, season_public_id=season_public_id, customized=False,
    )


@bp.post("/privacy-defaults/apply-to-visits")
def apply_privacy_defaults():
    _session_data, error = _session(csrf=True)
    if error:
        return error
    data = request.get_json(silent=True) or {}
    if data.get("confirmed") is not True:
        return _error("confirmation_required", "既存予定への反映を確認してください。", 400)
    return jsonify(updated_count=0, message="プロフィール設定はすべての予定に自動適用されます。")


@bp.get("/seasons")
def list_seasons():
    with get_engine().connect() as connection:
        rows = connection.execute(
            text(
                "SELECT public_id, name, slug, start_date, end_date FROM seasons "
                "WHERE is_active=1 ORDER BY start_date DESC"
            )
        ).mappings().all()
    return jsonify(seasons=[{
        **dict(row), "start_date": row["start_date"].isoformat(),
        "end_date": row["end_date"].isoformat(),
    } for row in rows])


def _visit(row) -> dict[str, object]:
    value = dict(row)
    value["visit_date"] = value["visit_date"].isoformat()
    value["arrival_time"] = _time_text(value["arrival_time"])
    return value


def _time_text(value: time | timedelta | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return value.isoformat()


_VISIT_SELECT = (
    "SELECT v.public_id, s.public_id AS season_public_id, s.name AS season_name, "
    "v.visit_date, v.park, v.arrival_time, v.costume, v.memo "
    "FROM visits v JOIN seasons s ON s.id=v.season_id "
)


def _visits_for(user_id: int):
    with get_engine().connect() as connection:
        return connection.execute(
            text(_VISIT_SELECT + "WHERE v.user_id=:id ORDER BY v.visit_date, v.id"),
            {"id": user_id},
        ).mappings().all()


@bp.get("/visits")
def list_visits():
    session, error = _session()
    if error:
        return error
    return jsonify(visits=[_visit(row) for row in _visits_for(int(session["user_id"]))])


@bp.get("/visits/<public_id>")
def get_visit(public_id: str):
    session, error = _session()
    if error:
        return error
    with get_engine().connect() as connection:
        row = connection.execute(
            text(_VISIT_SELECT + "WHERE v.public_id=:public_id AND v.user_id=:user_id"),
            {"public_id": public_id, "user_id": session["user_id"]},
        ).mappings().first()
    if not row:
        return _error("not_found", "予定が見つかりません。", 404)
    return jsonify(visit=_visit(row))


def _validate_visit(data: dict[str, object]) -> dict[str, object]:
    season_public_id = data.get("season_public_id")
    visit_date = data.get("visit_date")
    park = data.get("park")
    if not isinstance(season_public_id, str) or not isinstance(visit_date, str) or park not in PARKS:
        raise RegistrationError("シーズン、日付、パークを確認してください。")
    try:
        parsed_date = date.fromisoformat(visit_date)
    except ValueError as exc:
        raise RegistrationError("日付を確認してください。") from exc
    arrival = data.get("arrival_time") or None
    if arrival is not None:
        try:
            time.fromisoformat(str(arrival))
        except ValueError as exc:
            raise RegistrationError("到着時刻を確認してください。") from exc
    costume, memo = data.get("costume") or None, data.get("memo") or None
    if costume is not None and (not isinstance(costume, str) or len(costume) > 100):
        raise RegistrationError("服装は100文字以内で入力してください。")
    if memo is not None and (not isinstance(memo, str) or len(memo) > 500):
        raise RegistrationError("メモは500文字以内で入力してください。")
    return {
        "season_public_id": season_public_id, "visit_date": parsed_date, "park": park,
        "arrival_time": arrival, "costume": costume, "memo": memo,
        # Legacy columns remain populated until a later destructive schema cleanup.
        # Profile-level settings are the only source used for disclosure decisions.
        "visibility": "private", "detail_level": "full",
    }


def _save_visit(user_id: int, data: dict[str, object], public_id: str | None = None) -> str:
    values = _validate_visit(data)
    with get_engine().begin() as connection:
        season = connection.execute(
            text(
                "SELECT id, start_date, end_date FROM seasons "
                "WHERE public_id=:public_id AND is_active=1"
            ),
            {"public_id": values.pop("season_public_id")},
        ).mappings().first()
        if not season or not season["start_date"] <= values["visit_date"] <= season["end_date"]:
            raise RegistrationError("日付が選択したシーズンの期間外です。")
        values["season_id"] = season["id"]
        if public_id:
            result = connection.execute(
                text(
                    "UPDATE visits SET season_id=:season_id, visit_date=:visit_date, park=:park, "
                    "arrival_time=:arrival_time, costume=:costume, memo=:memo, "
                    "visibility=:visibility, detail_level=:detail_level "
                    "WHERE public_id=:public_id AND user_id=:user_id"
                ),
                {**values, "public_id": public_id, "user_id": user_id},
            )
            if result.rowcount != 1:
                raise RegistrationError("予定が見つかりません。")
            return public_id
        public_id = new_public_id()
        connection.execute(
            text(
                "INSERT INTO visits "
                "(public_id,user_id,season_id,visit_date,park,arrival_time,costume,memo,visibility,detail_level) "
                "VALUES (:public_id,:user_id,:season_id,:visit_date,:park,:arrival_time,:costume,:memo,:visibility,:detail_level)"
            ),
            {**values, "public_id": public_id, "user_id": user_id},
        )
        return public_id


@bp.post("/visits")
def create_visit():
    session, error = _session(csrf=True)
    if error:
        return error
    try:
        public_id = _save_visit(int(session["user_id"]), _payload())
    except RegistrationError as exc:
        return _error("invalid_visit", str(exc), 400)
    except IntegrityError:
        return _error("duplicate_visit", "同じシーズン・日付の予定は登録済みです。", 409)
    with get_engine().connect() as connection:
        row = connection.execute(
            text(_VISIT_SELECT + "WHERE v.public_id=:public_id AND v.user_id=:user_id"),
            {"public_id": public_id, "user_id": session["user_id"]},
        ).mappings().one()
    return jsonify(visit=_visit(row)), 201


@bp.patch("/visits/<public_id>")
def update_visit(public_id: str):
    session, error = _session(csrf=True)
    if error:
        return error
    with get_engine().connect() as connection:
        row = connection.execute(
            text(_VISIT_SELECT + "WHERE v.public_id=:public_id AND v.user_id=:user_id"),
            {"public_id": public_id, "user_id": session["user_id"]},
        ).mappings().first()
    if not row:
        return _error("not_found", "予定が見つかりません。", 404)
    try:
        _save_visit(int(session["user_id"]), {**_visit(row), **_payload()}, public_id)
    except RegistrationError as exc:
        return _error("invalid_visit", str(exc), 400)
    except IntegrityError:
        return _error("duplicate_visit", "同じシーズン・日付の予定は登録済みです。", 409)
    return get_visit(public_id)


@bp.delete("/visits/<public_id>")
def delete_visit(public_id: str):
    session, error = _session(csrf=True)
    if error:
        return error
    with get_engine().begin() as connection:
        result = connection.execute(
            text("DELETE FROM visits WHERE public_id=:public_id AND user_id=:user_id"),
            {"public_id": public_id, "user_id": session["user_id"]},
        )
    return ("", 204) if result.rowcount else _error("not_found", "予定が見つかりません。", 404)
