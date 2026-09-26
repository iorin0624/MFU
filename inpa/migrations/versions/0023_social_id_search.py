"""Publish privacy-aware social ID search.

Revision ID: 0023_social_id_search
Revises: 0022_security_credentials
Create Date: 2026-09-26
"""

from alembic import op

revision = "0023_social_id_search"
down_revision = "0022_security_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO app_releases (public_id,version,version_major,version_minor,version_patch,"
        "change_type,title,content_markdown,status,created_by,published_by,published_at,created_at,updated_at) "
        "VALUES ('BT909ZA0CCPS56PW23PGD2B6GT','1.5.0',1,5,0,'feature',"
        "'SNS IDでの利用者検索に対応',"
        "'つながり画面で、つながりIDに加えて、プロフィールで公開されているX・Instagram IDから利用者を検索できるようにしました。',"
        "'published','system','system',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) "
        "ON DUPLICATE KEY UPDATE title=VALUES(title),content_markdown=VALUES(content_markdown),"
        "status=VALUES(status),updated_at=UTC_TIMESTAMP(6)"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM user_release_dismissals WHERE release_id IN "
        "(SELECT id FROM app_releases WHERE version='1.5.0')"
    )
    op.execute("DELETE FROM app_releases WHERE version='1.5.0'")
