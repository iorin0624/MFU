"""Separate administrator page for pro/suc page-view analytics."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import ipaddress

from flask import jsonify, render_template, request

from app import admin_required
from app.pro_suc_pv import bp
from app.pro_suc_pv.analytics import summary


def _selected_range():
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date()
    choice = request.args.get("range", "today")
    if choice == "7d":
        return today - timedelta(days=6), today
    if choice == "30d":
        return today - timedelta(days=29), today
    if choice == "custom":
        try:
            start = datetime.strptime(request.args.get("start", ""), "%Y-%m-%d").date()
            end = datetime.strptime(request.args.get("end", ""), "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("開始日と終了日を指定してください") from None
        if start > end or (end - start).days > 366 or end > today:
            raise ValueError("期間は過去366日以内で指定してください")
        return start, end
    if choice != "today":
        raise ValueError("期間が正しくありません")
    return today, today


@bp.get("/admin/pro_suc_pv")
@admin_required
def index():
    return render_template("pro_suc_pv.html")


@bp.get("/admin/pro_suc_pv/data")
@admin_required
def data():
    try:
        start, end = _selected_range()
        site = request.args.get("site", "all")
        if site not in {"all", "pro", "suc"}:
            raise ValueError("サイトが正しくありません")
        result = summary(start, end, site, request.args.get("bots") == "1")
        result.update({"start": start.isoformat(), "end": end.isoformat(), "site": site})
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@bp.get("/admin/pro_suc_pv/ip-info")
@admin_required
def ip_info():
    value = request.args.get("ip", "")
    try:
        ip = str(ipaddress.ip_address(value))
    except ValueError:
        return jsonify({"error": "IPアドレスが正しくありません"}), 400
    from app.utils.db import get_db
    db = get_db()
    try:
        cur = db.cursor()
        cur.execute("SELECT 1 FROM pro_suc_page_views WHERE ip=%s LIMIT 1", (ip,))
        if not cur.fetchone():
            return jsonify({"error": "対象のアクセス元がありません"}), 404
    finally:
        db.close()
    from app.utils.whois_util import get_netinfo
    info = get_netinfo(ip) or {}
    return jsonify({
        "ip": ip,
        "country": str(info.get("country") or "不明")[:80],
        "provider": str(info.get("netname") or info.get("org") or "不明")[:160],
    })
