from __future__ import annotations

from urllib.parse import urlparse

from flask import abort, current_app, flash, redirect, render_template, request, send_from_directory, session, url_for

from app import _validate_csrf_request, admin_required

from . import profile_bp
from .avatar import PROFILE_AVATAR_ROOT, remove_profile_avatar, save_profile_avatar, valid_avatar_name
from .social_icons import (
    SOCIAL_ICON_ROOT, remove_social_icon, save_social_icon,
    valid_icon_file, validate_social_link_fields,
)
from .formatting import (
    format_qa_updated_at,
    linkify_plain_text_for_display,
    list_profile_qa_categories,
    normalize_plain_text_for_display,
    parse_profile_qa_text,
)
from .services import (
    add_known_work,
    create_default_main_profile_if_missing,
    delete_social_link,
    delete_known_work,
    get_main_profile,
    get_social_link,
    list_known_works,
    list_social_links,
    move_social_link,
    save_social_link,
    social_icon_is_visible,
    update_known_work,
    update_main_profile,
    update_main_profile_avatar,
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


def _avatar_url(profile: dict | None, *, public: bool = False) -> str | None:
    name = (profile or {}).get("avatar_file") or ""
    if not valid_avatar_name(name):
        return None
    path = url_for("profile.profile_avatar", name=name)
    return f"https://mfu.iori0624.jp{path}" if public else path


def _social_links_for_view(profile_id: int, *, public: bool = False) -> list[dict]:
    links = list_social_links(profile_id, visible_only=public)
    for link in links:
        name = link.get("icon_file") or ""
        if valid_icon_file(name):
            path = url_for("profile.social_icon_file", name=name)
            link["icon_url"] = f"https://mfu.iori0624.jp{path}" if public else path
        else:
            link["icon_url"] = None
    return links


@profile_bp.get("/profile/social-icon/<name>")
def social_icon_file(name: str):
    if not valid_icon_file(name) or not social_icon_is_visible(
        name, include_hidden=session.get("user") == "admin"
    ):
        abort(404)
    response = send_from_directory(SOCIAL_ICON_ROOT, name, max_age=0)
    response.cache_control.no_store = True
    return response


@profile_bp.get("/profile/avatar/<name>")
def profile_avatar(name: str):
    profile = get_main_profile()
    if (
        not valid_avatar_name(name)
        or not profile
        or (int(profile.get("is_public") or 0) != 1 and session.get("user") != "admin")
        or profile.get("avatar_file") != name
    ):
        abort(404)
    response = send_from_directory(PROFILE_AVATAR_ROOT, name, max_age=0)
    response.cache_control.no_store = True
    return response


@profile_bp.get("/profile")
def profile_public():
    profile = get_main_profile()
    if not profile or int(profile.get("is_public") or 0) != 1:
        abort(404)

    works = list_known_works(profile["id"]) if int(profile.get("show_known_works") or 0) == 1 else []
    grouped_works = _group_known_works(works)

    profile_view = dict(profile)
    profile_view["avatar_url"] = _avatar_url(profile, public=True)
    profile_view["intro_text"] = linkify_plain_text_for_display(profile.get("intro_text"))
    profile_view["request_notes_text"] = linkify_plain_text_for_display(profile.get("request_notes_text"))

    try:
        qa_items = parse_profile_qa_text(profile.get("qa_text"))
    except ValueError:
        current_app.logger.warning("Invalid profile Q&A text; hiding Q&A section")
        qa_items = []
    for item in qa_items:
        item["answer_html"] = linkify_plain_text_for_display(str(item["answer"]))

    social_links = _social_links_for_view(profile["id"], public=True) if profile.get("show_sns_links") else []
    return render_template(
        "profile_public.html", profile=profile_view, grouped_works=grouped_works,
        qa_items=qa_items, qa_categories=list_profile_qa_categories(qa_items),
        qa_updated_label=format_qa_updated_at(profile.get("qa_updated_at")),
        social_links=social_links,
    )


@profile_bp.route("/admin/profile", methods=["GET", "POST"])
@admin_required
def admin_profile_edit():
    create_default_main_profile_if_missing()

    if request.method == "POST":
        draft = dict(get_main_profile() or {})
        draft["avatar_url"] = _avatar_url(draft)
        for name in (
            "page_title", "display_name", "subtitle", "intro_text", "request_notes_text",
            "qa_text",
        ):
            draft[name] = request.form.get(name) or ""
        for name in ("is_public", "show_known_works", "show_sns_links"):
            draft[name] = _as_bool(name)

        errors: list[str] = []
        page_title, err = _clean_text("page_title", required=True, limit=255)
        if err:
            errors.append(err)

        display_name, err = _clean_text("display_name", required=True, limit=255)
        if err:
            errors.append(err)

        subtitle, err = _clean_text("subtitle", required=False, limit=255)
        if err:
            errors.append(err)

        intro_text_raw = (request.form.get("intro_text") or "").strip()
        request_notes_text_raw = (request.form.get("request_notes_text") or "").strip()
        qa_text_raw = (request.form.get("qa_text") or "").strip()

        intro_text = normalize_plain_text_for_display(intro_text_raw) or None
        request_notes_text = normalize_plain_text_for_display(request_notes_text_raw) or None
        qa_text = normalize_plain_text_for_display(qa_text_raw) or None
        try:
            parse_profile_qa_text(qa_text)
        except ValueError as exc:
            errors.append(str(exc))

        if errors:
            return render_template(
                "profile_admin_edit.html", profile=draft, form_errors=errors,
                social_links=_social_links_for_view(draft["id"]),
            ), 400

        update_main_profile(
            {
                "page_title": page_title,
                "display_name": display_name,
                "subtitle": subtitle,
                "intro_text": intro_text,
                "request_notes_text": request_notes_text,
                "qa_text": qa_text,
                "x_url": draft.get("x_url"),
                "instagram_url": draft.get("instagram_url"),
                "portfolio_url": draft.get("portfolio_url"),
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
        profile["avatar_url"] = _avatar_url(profile)
        profile["intro_text"] = normalize_plain_text_for_display(profile.get("intro_text"))
        profile["request_notes_text"] = normalize_plain_text_for_display(profile.get("request_notes_text"))
        profile["qa_text"] = normalize_plain_text_for_display(profile.get("qa_text"))

    return render_template(
        "profile_admin_edit.html", profile=profile,
        social_links=_social_links_for_view(profile["id"]),
    )


@profile_bp.post("/admin/profile/avatar")
@admin_required
def admin_profile_avatar_upload():
    csrf_error = _validate_csrf_request()
    if csrf_error is not None:
        return csrf_error
    try:
        name = save_profile_avatar(request.files.get("avatar_file"))
    except ValueError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("profile.admin_profile_edit"))

    try:
        previous = update_main_profile_avatar(name)
    except Exception:
        remove_profile_avatar(name)
        raise
    try:
        remove_profile_avatar(previous)
    except OSError:
        current_app.logger.warning("Could not remove old profile avatar", exc_info=True)
    flash("プロフィール写真を更新しました。", "success")
    return redirect(url_for("profile.admin_profile_edit"))


@profile_bp.post("/admin/profile/avatar/delete")
@admin_required
def admin_profile_avatar_delete():
    csrf_error = _validate_csrf_request()
    if csrf_error is not None:
        return csrf_error
    previous = update_main_profile_avatar(None)
    try:
        remove_profile_avatar(previous)
    except OSError:
        current_app.logger.warning("Could not remove deleted profile avatar", exc_info=True)
    flash("プロフィール写真を削除しました。", "success")
    return redirect(url_for("profile.admin_profile_edit"))


def _social_admin_redirect():
    return redirect(url_for("profile.admin_profile_edit") + "#profile-social-links")


def _save_social_link_from_request(link_id: int | None = None):
    csrf_error = _validate_csrf_request()
    if csrf_error is not None:
        return csrf_error
    profile = get_main_profile()
    existing = get_social_link(profile["id"], link_id) if link_id is not None else None
    if link_id is not None and existing is None:
        abort(404)

    uploaded = request.files.get("icon_file")
    has_upload = bool(uploaded and uploaded.filename)
    mode = (request.form.get("icon_mode") or "").strip()
    old_file = existing.get("icon_file") if existing else None
    try:
        label, url, mode, icon_name = validate_social_link_fields(
            label=request.form.get("label") or "",
            url=request.form.get("url") or "",
            icon_mode=mode,
            icon_name=request.form.get("icon_name") or "",
            has_custom_icon=has_upload or bool(old_file),
        )
    except ValueError as exc:
        flash(str(exc), "danger")
        return _social_admin_redirect()

    new_file = None
    try:
        if mode == "custom" and has_upload:
            new_file = save_social_icon(uploaded)
        icon_file = (new_file or old_file) if mode == "custom" else None
        payload = {
            "label": label,
            "url": url,
            "icon_mode": mode,
            "icon_name": icon_name,
            "icon_file": icon_file,
            "is_visible": _as_bool("is_visible"),
        }
        previous = save_social_link(profile["id"], payload, link_id=link_id)
    except ValueError as exc:
        if new_file:
            remove_social_icon(new_file)
        flash(str(exc), "danger")
        return _social_admin_redirect()
    except Exception:
        if new_file:
            remove_social_icon(new_file)
        raise

    if previous and previous != icon_file:
        try:
            remove_social_icon(previous)
        except OSError:
            current_app.logger.warning("Could not remove replaced social icon", exc_info=True)
    flash("リンクを保存しました。", "success")
    return _social_admin_redirect()


@profile_bp.post("/admin/profile/links/add")
@admin_required
def admin_profile_social_add():
    return _save_social_link_from_request()


@profile_bp.post("/admin/profile/links/<int:link_id>/update")
@admin_required
def admin_profile_social_update(link_id: int):
    return _save_social_link_from_request(link_id)


@profile_bp.post("/admin/profile/links/<int:link_id>/delete")
@admin_required
def admin_profile_social_delete(link_id: int):
    csrf_error = _validate_csrf_request()
    if csrf_error is not None:
        return csrf_error
    profile = get_main_profile()
    try:
        previous = delete_social_link(profile["id"], link_id)
    except LookupError:
        abort(404)
    try:
        remove_social_icon(previous)
    except OSError:
        current_app.logger.warning("Could not remove deleted social icon", exc_info=True)
    flash("リンクを削除しました。", "success")
    return _social_admin_redirect()


@profile_bp.post("/admin/profile/links/<int:link_id>/move/<direction>")
@admin_required
def admin_profile_social_move(link_id: int, direction: str):
    csrf_error = _validate_csrf_request()
    if csrf_error is not None:
        return csrf_error
    if direction not in {"up", "down"}:
        abort(404)
    profile = get_main_profile()
    moved = move_social_link(profile["id"], link_id, -1 if direction == "up" else 1)
    flash("表示順を変更しました。" if moved else "表示順は変更されませんでした。", "success" if moved else "info")
    return _social_admin_redirect()


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
