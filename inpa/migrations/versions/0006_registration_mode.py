"""Add an administrator-controlled invitation-only registration mode.

Revision ID: 0006_registration_mode
Revises: 0005_registration_invitations
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_registration_mode"
down_revision = "0005_registration_invitations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registration_settings",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("invite_only", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_by", sa.String(128), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id=1", name="chk_registration_settings_singleton"),
        sa.CheckConstraint("invite_only IN (0,1)", name="chk_registration_settings_invite_only"),
    )
    op.execute(sa.text(
        "INSERT INTO registration_settings (id,invite_only) VALUES (1,1)"
    ))


def downgrade() -> None:
    op.drop_table("registration_settings")
