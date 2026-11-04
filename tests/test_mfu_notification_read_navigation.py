from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_header_notification_navigation_does_not_mark_read():
    source = (ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    item_handler = source.split("const renderItems =", 1)[1].split("const escapeHtml =", 1)[0]
    assert "a.href = targetUrl" in item_handler
    assert "/read`" not in item_handler
    assert "preventDefault()" not in item_handler
    assert "/api/mfu-notifications/read-all" in source


def test_mfu_notification_links_leave_unread_until_explicit_action():
    source = (ROOT / "external_login_user" / "template" / "notifications.html").read_text(encoding="utf-8")
    assert "scope === 'mfu' && !isRead" in source
    assert 'data-mark-read>既読にする' in source
    assert "if (scope !== 'mfu') link.addEventListener('click'" in source
    assert "if (scope !== 'mfu') a.querySelector('[data-open]')" in source
    assert "a.querySelector('[data-mark-read]')?.addEventListener('click', markItemRead)" in source


def test_legacy_mfu_notification_list_also_navigates_without_reading():
    source = (ROOT / "templates" / "mfu_notifications.html").read_text(encoding="utf-8")
    assert "a.addEventListener('click'" not in source
    assert "/api/mfu-notifications/read-all" in source


def test_push_notification_opens_scoped_list_and_highlights_exact_item():
    worker = (ROOT / "external_login_user" / "notifications.py").read_text(encoding="utf-8")
    page = (ROOT / "external_login_user" / "template" / "notifications.html").read_text(encoding="utf-8")
    assert "data: {notification_id: payload.notification_id}" in worker
    assert "new URL('/mfu-notifications/', self.location.origin)" in worker
    assert "landing.searchParams.set('notification_id', String(id))" in worker
    assert "app.navigate(landing.toString())" in worker
    assert "clients.openWindow(landing.toString())" in worker
    assert "notification_id: int | None = None" in worker
    assert "where_sql += \" AND id=%s\"" in worker
    assert '@mfu_notifications_bp.get("/api/mfu-notifications/<int:notification_id>")' in worker
    assert "pushNotificationId ? 'all'" in page
    assert "onlyUnread.checked = false" in page
    assert "await highlightPushNotification()" in page
    assert "target.classList.add('is-push-target')" in page


def test_mfu_details_open_separately_without_automatic_read():
    page = (ROOT / "external_login_user" / "template" / "notifications.html").read_text(encoding="utf-8")
    assert 'target="_blank" rel="noopener noreferrer" data-open' in page
    assert "if (scope !== 'mfu') a.querySelector('[data-open]')" in page
    assert "if (scope !== 'mfu') await markItemRead()" not in page
