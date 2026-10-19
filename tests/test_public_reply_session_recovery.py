import ast
import base64
import json
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SECURITY_SOURCE = (ROOT / "utils" / "upload_security.py").read_text(encoding="utf-8")
APP_SOURCE = (ROOT / "__init__.py").read_text(encoding="utf-8")
REPLY_SOURCE = (ROOT / "utils" / "layer_reply.py").read_text(encoding="utf-8")
COOKIE_SOURCE = (ROOT / "utils" / "admin_session_cookie.py").read_text(encoding="utf-8")
VIEWER_SOURCE = (
    ROOT / "external_login_user" / "frontend" / "src" / "views" / "PublicUploadViewer.vue"
).read_text(encoding="utf-8")


def _load_compactor():
    module = ast.parse(SECURITY_SOURCE)
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "compact_public_auth_session"
    )
    namespace = {
        "VIEW_AUTH_SESSION_KEY": "view_auth_uuids",
        "VIEW_AUTH_VERSION_SESSION_KEY": "view_auth_versions",
        "VIEW_AUTH_MAX_ITEMS": 12,
        "LAYER_REPLY_UPLOAD_SESSION_KEY": "layer_reply_upload_auth",
        "LAYER_REPLY_UPLOAD_MAX_ITEMS": 6,
        "LAYER_REPLY_VIEW_SESSION_KEY": "layer_reply_view_auth",
        "LAYER_REPLY_VIEW_MAX_UPLOADS": 4,
        "LAYER_REPLY_VIEW_MAX_REPLIES": 4,
        "PUBLIC_AUTH_COOKIE_BUDGET": 3000,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), "upload_security.py", "exec"), namespace)
    return namespace["compact_public_auth_session"]


def _uuid(index):
    return f"{index:08x}-0000-4000-8000-000000000000"


class _Serializer:
    @staticmethod
    def dumps(payload):
        packed = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(packed).decode() + ("x" * 40)


def test_public_auth_grants_fit_cookie_budget_and_keep_recent_access():
    session = {
        "csrf_token": "x" * 43,
        "view_auth_uuids": [_uuid(index) for index in range(50)],
        "view_auth_versions": {_uuid(index): 1 for index in range(50)},
        "layer_reply_upload_auth": [_uuid(index) for index in range(60, 80)],
        "layer_reply_view_auth": {
            _uuid(index): [f"{reply:032x}" for reply in range(10)]
            for index in range(80, 90)
        },
    }
    app = SimpleNamespace(session_interface=SimpleNamespace(get_signing_serializer=lambda _app: _Serializer()))

    _load_compactor()(session, app)

    assert len(_Serializer.dumps(session)) <= 3000
    assert session["view_auth_uuids"][-1] == _uuid(49)
    assert len(session["view_auth_uuids"]) <= 12
    assert set(session["view_auth_versions"]) == set(session["view_auth_uuids"])
    assert session["layer_reply_upload_auth"][-1] == _uuid(79)
    assert len(session["layer_reply_upload_auth"]) <= 6
    assert len(session["layer_reply_view_auth"]) <= 4
    assert all(len(replies) <= 4 for replies in session["layer_reply_view_auth"].values())


def test_reply_upload_retries_only_after_pre_save_csrf_rejection():
    assert "compact_public_auth_session(session, app)" in COOKIE_SOURCE
    assert 'if request.endpoint == "layer_reply.public_replies":' in APP_SOURCE
    assert '"error": "csrf_failed"' in APP_SOURCE
    assert '@layer_reply_bp.get("/view/<uuid>/replies/session")' in REPLY_SOURCE
    assert "None if upload_auth_method(upload) == AUTH_ACCESS_TOKEN" in REPLY_SOURCE
    assert 'error="auth_required"' in REPLY_SOURCE
    assert "await refreshReplySession();" in VIEWER_SOURCE
    assert "reason instanceof ReplyCsrfError" in VIEWER_SOURCE
    assert "if (!(reason instanceof ReplyCsrfError)) throw reason;" in VIEWER_SOURCE
    assert "await sendReply(form);" in VIEWER_SOURCE
    assert 'target="_blank" rel="noopener noreferrer"' in VIEWER_SOURCE
    assert "replyFiles.value = []" not in VIEWER_SOURCE.split("async function submitReply()", 1)[1].split("function openReplyLightbox", 1)[0]
