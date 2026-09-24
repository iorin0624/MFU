"""Publish individual profile calendars.

Revision ID: 0015_person_calendars
Revises: 0014_app_releases
Create Date: 2026-09-25
"""

from alembic import op

revision = "0015_person_calendars"
down_revision = "0014_app_releases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO app_releases (public_id,version,version_major,version_minor,version_patch,"
        "change_type,title,content_markdown,status,created_by,published_by,published_at,created_at,updated_at) "
        "VALUES ('Q775RHYT2M45SB0BRP72AVGKTX','1.2.0',1,2,0,'feature',"
        "'個別プロフィールとカレンダーを追加',"
        "'つながり画面のニックネームから、利用者のプロフィールと公開予定カレンダーを確認できるようにしました。XとInstagramは利用者が表示を許可している場合だけ表示します。',"
        "'published','system','system',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM user_release_dismissals WHERE release_id IN "
        "(SELECT id FROM app_releases WHERE version='1.2.0')"
    )
    op.execute("DELETE FROM app_releases WHERE version='1.2.0'")
