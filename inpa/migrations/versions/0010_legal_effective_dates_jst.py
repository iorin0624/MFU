"""Correct existing administrator legal dates from JST midnight to UTC.

Revision ID: 0010_legal_dates_jst
Revises: 0009_identity_legal
Create Date: 2026-09-24
"""

from alembic import op

revision = "0010_legal_dates_jst"
down_revision = "0009_identity_legal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE legal_documents SET effective_at=DATE_SUB(effective_at, INTERVAL 9 HOUR), "
        "updated_at=UTC_TIMESTAMP(6) WHERE effective_at IS NOT NULL "
        "AND TIME(effective_at)='00:00:00' AND published_by<>'migration'"
    )
    op.execute(
        "UPDATE legal_documents older JOIN legal_documents newer "
        "ON newer.document_type=older.document_type AND newer.status='published' "
        "AND newer.effective_at<=UTC_TIMESTAMP(6) AND (newer.effective_at>older.effective_at "
        "OR (newer.effective_at=older.effective_at AND newer.id>older.id)) "
        "SET older.status='retired',older.updated_at=UTC_TIMESTAMP(6) "
        "WHERE older.status='published' AND older.effective_at<=UTC_TIMESTAMP(6)"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE legal_documents SET status='published' "
        "WHERE status='retired' AND published_at IS NOT NULL"
    )
    op.execute(
        "UPDATE legal_documents SET effective_at=DATE_ADD(effective_at, INTERVAL 9 HOUR), "
        "updated_at=UTC_TIMESTAMP(6) WHERE effective_at IS NOT NULL "
        "AND TIME(effective_at)='15:00:00' AND published_by<>'migration'"
    )
