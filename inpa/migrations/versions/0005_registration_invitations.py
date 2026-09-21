"""Require single-use administrator invitations for registration.

Revision ID: 0005_registration_invitations
Revises: 0004_profile_privacy_matrix
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0005_registration_invitations"
down_revision = "0004_profile_privacy_matrix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registration_invitations",
        sa.Column("id", mysql.BIGINT(unsigned=True), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.CHAR(26), nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("token_last4", sa.CHAR(4), nullable=False),
        sa.Column("memo", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("claimed_email_normalized", sa.String(254), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("used_by_user_id", mysql.BIGINT(unsigned=True), nullable=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(128), nullable=True),
        sa.ForeignKeyConstraint(
            ["used_by_user_id"], ["users.id"], ondelete="RESTRICT", name="fk_registration_invitation_user"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_registration_invitations_public_id"),
        sa.UniqueConstraint("token_hash", name="uq_registration_invitations_token"),
        sa.UniqueConstraint("used_by_user_id", name="uq_registration_invitations_user"),
        sa.CheckConstraint(
            "status IN ('active','claimed','used','revoked')", name="chk_registration_invitations_status"
        ),
    )
    op.create_index(
        "ix_registration_invitations_status_expiry",
        "registration_invitations",
        ["status", "expires_at"],
    )
    op.add_column(
        "registration_requests",
        sa.Column("invitation_id", mysql.BIGINT(unsigned=True), nullable=True),
    )
    op.create_index(
        "ix_registration_requests_invitation", "registration_requests", ["invitation_id"]
    )
    op.create_foreign_key(
        "fk_registration_request_invitation",
        "registration_requests",
        "registration_invitations",
        ["invitation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Links created before invitation-only registration must not remain usable.
    op.execute(sa.text(
        "UPDATE registration_requests SET consumed_at=CURRENT_TIMESTAMP(6) WHERE consumed_at IS NULL"
    ))


def downgrade() -> None:
    op.drop_constraint(
        "fk_registration_request_invitation", "registration_requests", type_="foreignkey"
    )
    op.drop_index("ix_registration_requests_invitation", table_name="registration_requests")
    op.drop_column("registration_requests", "invitation_id")
    op.drop_index(
        "ix_registration_invitations_status_expiry", table_name="registration_invitations"
    )
    op.drop_table("registration_invitations")
