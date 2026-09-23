"""Normalize seeded legal effective dates to JST midnight.

Revision ID: 0011_seed_legal_jst
Revises: 0010_legal_dates_jst
Create Date: 2026-09-24
"""

from alembic import op

revision = "0011_seed_legal_jst"
down_revision = "0010_legal_dates_jst"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE legal_documents SET effective_at="
        "DATE_SUB(DATE(DATE_ADD(published_at, INTERVAL 9 HOUR)), INTERVAL 9 HOUR), "
        "updated_at=UTC_TIMESTAMP(6) WHERE published_by='migration' AND published_at IS NOT NULL"
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
