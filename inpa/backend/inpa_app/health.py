from __future__ import annotations

from flask import Blueprint, jsonify
from sqlalchemy.exc import SQLAlchemyError

from .db import ping

bp = Blueprint("health", __name__)


@bp.get("/health/live")
def live():
    return jsonify(status="ok")


@bp.get("/health/ready")
def ready():
    try:
        ping()
    except SQLAlchemyError:
        return jsonify(status="not_ready"), 503
    return jsonify(status="ok")
