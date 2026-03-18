from __future__ import annotations

from urllib.parse import urlparse

from flask import abort, flash, redirect, render_template, request, url_for

from app import admin_required

from . import profile_bp
from .formatting import linkify_plain_text_for_display, normalize_plain_text_for_display
from .services import (
    add_known_work,
    create_default_main_profile_if_missing,
    delete_known_work,
    get_main_profile,
    list_known_works,
    update_known_work,
    update_main_profile,
)


def _as_bool(name: str) -> int:
    return 1 if request.form.get(name) in {"1", "on", "true", "True"} else 0


def _as_int(name: str, default: int = 0) -> int:
    raw = (request.form.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _clean_text(name: str, required: bool = False, limit: int | None = None) -> tuple[str | None, str | None]:
    value = (request.form.get(name) or "").strip()
    if required and not value:
        return None, f"{name} は必須です。"
    if limit is not None and len(value) > limit:
        return None, f"{name} は{limit}文字以内で入力してください。"
    return (value or None), None


def _clean_url(name: str) -> tuple[str | None, str | None]:
    value = (request.form.get(name) or "").strip()
    if not value:
        return None, None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, f"{name} は http:// または https:// のURLを入力してください。"
    if len(value) > 500:
        return None, f"{name} は500文字以内で入力してください。"
    return value, None



def _group_known_works(rows: list[dict]) -> list[dict]:
    grouped: list[dict] = []
    seen: dict[str, dict] = {}
    for row in rows:
        key = row["category_name"]
        if key not in seen:
            bucket = {
                "category_name": row["category_name"],
                "category_sort": row["category_sort"],
                "items": [],
            }
            seen[key] = bucket
            grouped.append(bucket)
        seen[key]["items"].append(row)
    return grouped


@profile_bp.get("/profile")
def profile_public():
    profile = get_main_profile()
    if not profile or int(profile.get("is_public") or 0) != 1:
        abort(404)

    works = list_known_works(profile["id"]) if int(profile.get("show_known_works") or 0) == 1 else []
    grouped_works = _group_known_works(works)

    profile_view = dict(profile)
    profile_view["intro_text"] = linkify_plain_text_for_display(profile.get("intro_text"))
    profile_view["request_notes_text"] = linkify_plain_text_for_display(profile.get("request_notes_text"))

    return render_template("profile_public.html", profile=profile_view, grouped_works=grouped_works)


@profile_bp.route("/admin/profile", methods=["GET", "POST"])
@admin_required
def admin_profile_edit():
    create_default_main_profile_if_missing()

    if request.method == "POST":
        page_title, err = _clean_text("page_title", required=True, limit=255)
        if err:
            flash(err, "danger")
            return redirect(url_for("profile.admin_profile_edit"))

        display_name, err = _clean_text("display_name", required=True, limit=255)
        if err:
            flash(err, "danger")
            return redirect(url_for("profile.admin_profile_edit"))

        subtitle, err = _clean_text("subtitle", required=False, limit=255)
        if err:
            flash(err, "danger")
            return redirect(url_for("profile.admin_profile_edit"))

        intro_text_raw = (request.form.get("intro_text") or "").strip()
        request_notes_text_raw = (request.form.get("request_notes_text") or "").strip()

        intro_text = normalize_plain_text_for_display(intro_text_raw) or None
        request_notes_text = normalize_plain_text_for_display(request_notes_text_raw) or None

        x_url, err = _clean_url("x_url")
        if err:
            flash(err, "danger")
            return redirect(url_for("profile.admin_profile_edit"))

        instagram_url, err = _clean_url("instagram_url")
        if err:
            flash(err, "danger")
            return redirect(url_for("profile.admin_profile_edit"))

        portfolio_url, err = _clean_url("portfolio_url")
        if err:
            flash(err, "danger")
            return redirect(url_for("profile.admin_profile_edit"))

        update_main_profile(
            {
                "page_title": page_title,
                "display_name": display_name,
                "subtitle": subtitle,
                "intro_text": intro_text,
                "request_notes_text": request_notes_text,
                "x_url": x_url,
                "instagram_url": instagram_url,
                "portfolio_url": portfolio_url,
                "is_public": _as_bool("is_public"),
                "show_known_works": _as_bool("show_known_works"),
                "show_sns_links": _as_bool("show_sns_links"),
            }
        )
        flash("プロフィール情報を更新しました。", "success")
        return redirect(url_for("profile.admin_profile_edit"))

    profile = get_main_profile()
    if profile:
        profile = dict(profile)
        profile["intro_text"] = normalize_plain_text_for_display(profile.get("intro_text"))
        profile["request_notes_text"] = normalize_plain_text_for_display(profile.get("request_notes_text"))

    return render_template("profile_admin_edit.html", profile=profile)


@profile_bp.get("/admin/profile/works")
@admin_required
def admin_profile_works():
    create_default_main_profile_if_missing()
    profile = get_main_profile()
    works = list_known_works(profile["id"])
    return render_template("profile_admin_works.html", profile=profile, works=works)


@profile_bp.post("/admin/profile/works/add")
@admin_required
def admin_profile_works_add():
    profile = get_main_profile()
    if not profile:
        create_default_main_profile_if_missing()
        profile = get_main_profile()

    category_name, err = _clean_text("category_name", required=True, limit=100)
    if err:
        flash(err, "danger")
        return redirect(url_for("profile.admin_profile_works"))

    item_name, err = _clean_text("item_name", required=True, limit=255)
    if err:
        flash(err, "danger")
        return redirect(url_for("profile.admin_profile_works"))

    add_known_work(
        profile["id"],
        category_name,
        item_name,
        _as_int("category_sort", 0),
        _as_int("item_sort", 0),
    )
    flash("作品を追加しました。", "success")
    return redirect(url_for("profile.admin_profile_works"))


@profile_bp.post("/admin/profile/works/<int:work_id>/update")
@admin_required
def admin_profile_works_update(work_id: int):
    profile = get_main_profile()
    if not profile:
        abort(404)

    category_name, err = _clean_text("category_name", required=True, limit=100)
    if err:
        flash(err, "danger")
        return redirect(url_for("profile.admin_profile_works"))

    item_name, err = _clean_text("item_name", required=True, limit=255)
    if err:
        flash(err, "danger")
        return redirect(url_for("profile.admin_profile_works"))

    ok = update_known_work(
        work_id,
        profile["id"],
        category_name,
        item_name,
        _as_int("category_sort", 0),
        _as_int("item_sort", 0),
    )
    flash("作品を更新しました。" if ok else "対象作品が見つかりませんでした。", "success" if ok else "warning")
    return redirect(url_for("profile.admin_profile_works"))


@profile_bp.post("/admin/profile/works/<int:work_id>/delete")
@admin_required
def admin_profile_works_delete(work_id: int):
    profile = get_main_profile()
    if not profile:
        abort(404)

    ok = delete_known_work(work_id, profile["id"])
    flash("作品を削除しました。" if ok else "対象作品が見つかりませんでした。", "success" if ok else "warning")
    return redirect(url_for("profile.admin_profile_works"))