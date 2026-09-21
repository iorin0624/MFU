"""Add configurable warning-only restriction periods.

Revision ID: 0003_restriction_periods
Revises: 0002_share_token_recovery
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0003_restriction_periods"
down_revision = "0002_share_token_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "restriction_periods",
        sa.Column("id", mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("season_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("restriction_type", sa.String(32), nullable=False, server_default="costume_prohibited"),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("park_scope", sa.String(16), nullable=False, server_default="all"),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("enforcement", sa.String(16), nullable=False, server_default="warning"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], ondelete="CASCADE", name="fk_restrictions_season"),
        sa.UniqueConstraint("public_id", name="uq_restrictions_public_id"),
        sa.CheckConstraint("start_date <= end_date", name="chk_restrictions_dates"),
        sa.CheckConstraint("park_scope IN ('all','land','sea')", name="chk_restrictions_park"),
        sa.CheckConstraint("enforcement = 'warning'", name="chk_restrictions_enforcement"),
    )
    op.create_index(
        "ix_restrictions_season_dates",
        "restriction_periods",
        ["season_id", "is_active", "start_date", "end_date"],
    )


def downgrade() -> None:
    op.drop_table("restriction_periods")
