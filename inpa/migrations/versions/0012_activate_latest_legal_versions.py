"""Activate the latest eligible legal version after JST correction.

Revision ID: 0012_latest_legal
Revises: 0011_seed_legal_jst
Create Date: 2026-09-24
"""

from alembic import op

revision = "0012_latest_legal"
down_revision = "0011_seed_legal_jst"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE legal_documents document JOIN ("
        "SELECT document_type,MAX(id) AS latest_id FROM legal_documents "
        "WHERE published_at IS NOT NULL AND effective_at<=UTC_TIMESTAMP(6) "
        "GROUP BY document_type) latest ON latest.document_type=document.document_type "
        "SET document.status=IF(document.id=latest.latest_id,'published','retired'),"
        "document.updated_at=UTC_TIMESTAMP(6) "
        "WHERE document.published_at IS NOT NULL AND document.effective_at<=UTC_TIMESTAMP(6)"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE legal_documents SET status='published' "
        "WHERE published_at IS NOT NULL AND effective_at<=UTC_TIMESTAMP(6)"
    )
