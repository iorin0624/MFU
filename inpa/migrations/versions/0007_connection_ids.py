"""Add short connection IDs for user-facing lookup.

Revision ID: 0007_connection_ids
Revises: 0006_registration_mode
Create Date: 2026-09-21
"""

import secrets

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0007_connection_ids"
down_revision = "0006_registration_mode"
branch_labels = None
depends_on = None

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _new_id(used: set[str]) -> str:
    while True:
        value = "".join(secrets.choice(_ALPHABET) for _ in range(8))
        if value not in used:
            used.add(value)
            return value


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "connection_id", mysql.CHAR(8, charset="ascii", collation="ascii_bin"), nullable=True
        ),
    )
    connection = op.get_bind()
    user_ids = connection.execute(sa.text("SELECT id FROM users ORDER BY id")).scalars().all()
    used: set[str] = set()
    for user_id in user_ids:
        connection.execute(
            sa.text("UPDATE users SET connection_id=:connection_id WHERE id=:id"),
            {"connection_id": _new_id(used), "id": user_id},
        )
    op.alter_column(
        "users",
        "connection_id",
        existing_type=mysql.CHAR(8, charset="ascii", collation="ascii_bin"),
        nullable=False,
    )
    op.create_unique_constraint("uq_users_connection_id", "users", ["connection_id"])


def downgrade() -> None:
    op.drop_constraint("uq_users_connection_id", "users", type_="unique")
    op.drop_column("users", "connection_id")
