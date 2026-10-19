import ast
import base64
import hashlib
import hmac
import os
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]


def _token_functions():
    path = ROOT / "utils" / "upload_security.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {
        "_upload_access_token_secret",
        "generate_upload_access_token",
        "hash_upload_access_token",
        "create_upload_access_token_hash",
        "verify_upload_access_token",
        "build_upload_view_url",
    }
    chosen = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {
        "base64": base64,
        "hashlib": hashlib,
        "hmac": hmac,
        "os": os,
        "re": re,
        "current_app": SimpleNamespace(config={}, secret_key="stable-upload-access-token-test-secret"),
        "AUTH_ACCESS_TOKEN": "access_token",
        "UPLOAD_ACCESS_TOKEN_PREFIX": "mfu_view_",
        "normalize_upload_auth_method": lambda method: method,
        "upload_auth_method": lambda upload: upload.get("auth_method"),
    }
    exec(compile(ast.Module(body=chosen, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def test_new_access_url_reuses_the_existing_token_without_a_fragment():
    functions = _token_functions()
    upload = {"uuid": "a" * 32, "auth_method": "access_token"}

    existing_token = functions["generate_upload_access_token"](upload["uuid"])
    upload["access_token_hash"] = functions["create_upload_access_token_hash"](
        upload["uuid"], upload["auth_method"]
    )
    url = urlsplit(functions["build_upload_view_url"]("https://mfu.example/", upload))

    assert url.path == f"/view/{upload['uuid']}/access"
    assert url.fragment == ""
    assert parse_qs(url.query) == {"token": [existing_token]}
    assert functions["verify_upload_access_token"](upload, existing_token)
    # A previously issued # URL still contains the same valid token.
    old_url = urlsplit(f"https://mfu.example/view/{upload['uuid']}/access#{existing_token}")
    assert functions["verify_upload_access_token"](upload, old_url.fragment)


def test_non_token_upload_url_is_unchanged():
    url = _token_functions()["build_upload_view_url"](
        "https://mfu.example/", {"uuid": "b" * 32, "auth_method": "password"}
    )
    assert url == f"https://mfu.example/view/{'b' * 32}"


def test_access_page_accepts_new_and_legacy_urls_and_strips_token_from_history():
    template = (ROOT / "templates" / "view_access_token.html").read_text(encoding="utf-8")
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")

    assert "new URLSearchParams(window.location.search).get('token')" in template
    assert "window.location.hash.startsWith('#')" in template
    assert "history.replaceState(null, '', window.location.pathname)" in template
    assert 'name="referrer" content="no-referrer"' in template
    assert 'response.headers["Referrer-Policy"] = "no-referrer"' in source


def test_upload_access_url_is_redacted_from_application_referer_logs():
    path = ROOT / "utils" / "logs.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "redact_upload_access_url"
    )
    namespace = {"urlsplit": urlsplit, "urlunsplit": urlunsplit}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec"), namespace)
    redact = namespace["redact_upload_access_url"]

    access_url = "https://mfu.example/view/" + "a" * 32 + "/access?token=secret&src=dm"
    assert redact(access_url) == access_url.split("?", 1)[0]
    assert redact("https://mfu.example/view/abc") == "https://mfu.example/view/abc"
