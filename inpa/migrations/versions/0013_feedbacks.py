"""Add user feedback workflow.

Revision ID: 0013_feedbacks
Revises: 0012_latest_legal
Create Date: 2026-09-24
"""

from alembic import op

revision = "0013_feedbacks"
down_revision = "0012_latest_legal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE feedbacks ("
        "id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
        "public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,"
        "user_id BIGINT UNSIGNED NOT NULL,"
        "sender_public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,"
        "sender_connection_id CHAR(8) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,"
        "sender_display_name VARCHAR(40) NOT NULL,"
        "sender_email VARCHAR(254) NOT NULL,"
        "category VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,"
        "message TEXT NOT NULL,source_path VARCHAR(500) NULL,user_agent VARCHAR(512) NULL,"
        "status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'new',"
        "admin_memo TEXT NULL,handled_by VARCHAR(128) NULL,handled_at DATETIME(6) NULL,"
        "created_at DATETIME(6) NOT NULL,updated_at DATETIME(6) NOT NULL,"
        "PRIMARY KEY (id),UNIQUE KEY uq_feedbacks_public_id (public_id),"
        "KEY ix_feedbacks_user_created (user_id,created_at),"
        "KEY ix_feedbacks_status_created (status,created_at),"
        "KEY ix_feedbacks_category_created (category,created_at),"
        "CONSTRAINT fk_feedbacks_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT,"
        "CONSTRAINT ck_feedbacks_category CHECK (category IN ('bug','feature','usability','wording','other')),"
        "CONSTRAINT ck_feedbacks_status CHECK (status IN ('new','in_progress','resolved','dismissed'))"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )


def downgrade() -> None:
    op.execute("DROP TABLE feedbacks")
