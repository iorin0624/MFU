"""Add profile-level audience and field visibility settings.

Revision ID: 0004_profile_privacy_matrix
Revises: 0003_restriction_periods
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0004_profile_privacy_matrix"
down_revision = "0003_restriction_periods"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_privacy_settings",
        sa.Column("user_id", mysql.BIGINT(unsigned=True), nullable=False),
        sa.Column("audience", sa.String(16), nullable=False),
        sa.Column("show_date", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("show_park", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("show_costume", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("show_memo", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE", name="fk_privacy_user"),
        sa.PrimaryKeyConstraint("user_id", "audience", name="pk_user_privacy_settings"),
        sa.CheckConstraint("audience IN ('link','logged_in','mutual','private')", name="chk_privacy_audience"),
    )
    field_values = {
        "show_date": "1",
        "show_park": "CASE WHEN default_detail_level IN ('park','memo','full') THEN 1 ELSE 0 END",
        "show_costume": "CASE WHEN default_detail_level='full' THEN 1 ELSE 0 END",
        "show_memo": "CASE WHEN default_detail_level IN ('memo','full') THEN 1 ELSE 0 END",
    }
    for audience, visibilities in (
        ("link", "'link'"),
        ("logged_in", "'logged_in'"),
        ("mutual", "'following','mutual'"),
        ("private", "'private'"),
    ):
        op.execute(sa.text(
            "INSERT INTO user_privacy_settings "
            "(user_id,audience,show_date,show_park,show_costume,show_memo) "
            f"SELECT id,'{audience}',{field_values['show_date']},{field_values['show_park']},"
            f"{field_values['show_costume']},{field_values['show_memo']} FROM users "
            f"WHERE default_visibility IN ({visibilities})"
        ))
        op.execute(sa.text(
            "INSERT INTO user_privacy_settings (user_id,audience) "
            f"SELECT id,'{audience}' FROM users WHERE default_visibility NOT IN ({visibilities})"
        ))
    # Owners previously always saw every field in their own shared/calendar view.
    op.execute(sa.text(
        "UPDATE user_privacy_settings SET show_date=1,show_park=1,show_costume=1,show_memo=1 "
        "WHERE audience='private'"
    ))


def downgrade() -> None:
    op.drop_table("user_privacy_settings")
