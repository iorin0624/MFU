"""Store share tokens encrypted so owners can retrieve their current short URL.

Revision ID: 0002_share_token_recovery
Revises: 0001_initial_schema
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_share_token_recovery"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("share_tokens", sa.Column("token_ciphertext", sa.LargeBinary(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("share_tokens", "token_ciphertext")
