"""Create the INPA V1 initial schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-21
"""

from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def _schema_statements() -> list[str]:
    # The SQL source is immutable once this revision is deployed. It contains
    # no procedure bodies or literals with semicolons, so statement splitting
    # is deterministic for this initial MySQL-only migration.
    source = Path(__file__).parents[1] / "0001_initial_schema.sql"
    return [statement.strip() for statement in source.read_text(encoding="utf-8").split(";") if statement.strip()]


def upgrade() -> None:
    for statement in _schema_statements():
        op.execute(sa.text(statement))


def downgrade() -> None:
    for table_name in (
        "account_deletion_requests",
        "admin_audit_logs",
        "admin_api_nonces",
        "rate_limit_counters",
        "mail_logs",
        "security_events",
        "reports",
        "share_tokens",
        "blocks",
        "follows",
        "visits",
        "seasons",
        "password_resets",
        "email_verifications",
        "user_sessions",
        "registration_requests",
        "users",
    ):
        op.drop_table(table_name)
