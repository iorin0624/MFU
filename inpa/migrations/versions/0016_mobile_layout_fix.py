"""Publish mobile layout fixes.

Revision ID: 0016_mobile_layout_fix
Revises: 0015_person_calendars
Create Date: 2026-09-25
"""

from alembic import op

revision = "0016_mobile_layout_fix"
down_revision = "0015_person_calendars"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO app_releases (public_id,version,version_major,version_minor,version_patch,"
        "change_type,title,content_markdown,status,created_by,published_by,published_at,created_at,updated_at) "
        "VALUES ('574XXVJASGBDRYNAGMD4NBDWWB','1.2.1',1,2,1,'fix',"
        "'スマートフォン表示を調整',"
        "'トップ、予定、統合カレンダーの左右余白と中央配置を揃え、入力フォームの横幅を統一しました。',"
        "'published','system','system',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM user_release_dismissals WHERE release_id IN "
        "(SELECT id FROM app_releases WHERE version='1.2.1')"
    )
    op.execute("DELETE FROM app_releases WHERE version='1.2.1'")
