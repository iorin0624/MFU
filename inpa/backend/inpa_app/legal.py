"""Published legal documents and immutable user consent history."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from .auth.routes import _error, _require_session
from .auth.service import verify_csrf
from .db import get_engine

bp = Blueprint("legal", __name__, url_prefix="/api/v1/legal")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def current_documents(connection) -> dict[str, dict[str, object]]:
    rows = connection.execute(text(
        "SELECT id,public_id,document_type,version,title,content_markdown,content_sha256,"
        "requires_reconsent,effective_at,published_at FROM legal_documents "
        "WHERE status='published' AND effective_at<=:now "
        "ORDER BY document_type,effective_at DESC,id DESC"
    ), {"now": _now()}).mappings().all()
    result = {}
    for row in rows:
        result.setdefault(row["document_type"], dict(row))
    return result


def missing_consents(connection, user_id: int) -> list[dict[str, object]]:
    documents = current_documents(connection)
    missing = []
    for document_type, document in documents.items():
        consented = connection.execute(text(
            "SELECT 1 FROM user_legal_consents WHERE user_id=:user_id "
            "AND legal_document_id=:document_id"
        ), {"user_id": user_id, "document_id": document["id"]}).first()
        if consented:
            continue
        prior = connection.execute(text(
            "SELECT 1 FROM user_legal_consents c JOIN legal_documents d ON d.id=c.legal_document_id "
            "WHERE c.user_id=:user_id AND d.document_type=:document_type LIMIT 1"
        ), {"user_id": user_id, "document_type": document_type}).first()
        if not prior or document["requires_reconsent"]:
            missing.append({key: value for key, value in document.items() if key != "id"})
    return missing


@bp.get("/documents")
def documents():
    with get_engine().connect() as connection:
        values = current_documents(connection)
    return jsonify(documents={key: {k: v for k, v in value.items() if k != "id"}
                              for key, value in values.items()})


@bp.get("/documents/<document_type>")
def document(document_type: str):
    if document_type not in {"terms", "privacy"}:
        return _error("not_found", "文書が見つかりません。", 404)
    with get_engine().connect() as connection:
        value = current_documents(connection).get(document_type)
    if not value:
        return _error("not_found", "文書が見つかりません。", 404)
    return jsonify(document={key: val for key, val in value.items() if key != "id"})


@bp.get("/consent-status")
def consent_status():
    session, error = _require_session(enforce_legal=False)
    if error:
        return error
    with get_engine().connect() as connection:
        missing = missing_consents(connection, int(session["user_id"]))
    return jsonify(required=bool(missing), missing=missing)


@bp.post("/consents")
def consent():
    session, error = _require_session(enforce_legal=False)
    if error:
        return error
    if not verify_csrf(session, request.headers.get("X-CSRF-Token")):
        return _error("csrf_failed", "リクエストを確認できませんでした。", 403)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("terms_accepted") is not True or data.get("privacy_accepted") is not True:
        return _error("invalid_request", "2つの文書への同意が必要です。", 400)
    try:
        ip_value = ipaddress.ip_address(request.remote_addr or "").packed
    except ValueError:
        ip_value = None
    now = _now()
    with get_engine().begin() as connection:
        values = current_documents(connection)
        if (
            data.get("terms_document_id") != values.get("terms", {}).get("public_id")
            or data.get("privacy_document_id") != values.get("privacy", {}).get("public_id")
        ):
            return _error("document_changed", "法務文書が更新されました。内容を再確認してください。", 409)
        for value in values.values():
            exists = connection.execute(text(
                "SELECT 1 FROM user_legal_consents WHERE user_id=:user_id "
                "AND legal_document_id=:document_id"
            ), {"user_id": session["user_id"], "document_id": value["id"]}).first()
            if exists:
                continue
            connection.execute(text(
                "INSERT INTO user_legal_consents "
                "(user_id,legal_document_id,consented_at,consent_method,ip_address,user_agent) "
                "VALUES (:user_id,:document_id,:now,'reconsent',:ip,:agent)"
            ), {"user_id": session["user_id"], "document_id": value["id"], "now": now,
                "ip": ip_value, "agent": request.user_agent.string[:512] or None})
    return jsonify(message="同意を保存しました。")
