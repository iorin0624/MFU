"""Add per-season audience and field visibility settings.

Revision ID: 0008_season_privacy_settings
Revises: 0007_connection_ids
Create Date: 2026-09-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0008_season_privacy_settings"
down_revision = "0007_connection_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_season_privacy_settings",
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("season_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("show_date", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("show_park", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("show_costume", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("show_memo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="fk_season_privacy_user"),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], ondelete="CASCADE", name="fk_season_privacy_season"),
        sa.PrimaryKeyConstraint("user_id", "season_id", "audience", name="pk_user_season_privacy"),
        sa.CheckConstraint("audience IN ('link','logged_in','mutual','private')", name="chk_season_privacy_audience"),
    )
    op.execute(sa.text(
        "INSERT INTO user_season_privacy_settings "
        "(user_id,season_id,audience,show_date,show_park,show_costume,show_memo) "
        "SELECT p.user_id,s.id,p.audience,p.show_date,p.show_park,p.show_costume,p.show_memo "
        "FROM user_privacy_settings p CROSS JOIN seasons s"
    ))


def downgrade() -> None:
    op.drop_table("user_season_privacy_settings")
