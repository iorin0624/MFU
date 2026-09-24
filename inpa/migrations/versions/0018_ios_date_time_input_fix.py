"""Publish iOS date and time input layout fixes.

Revision ID: 0018_ios_date_time_input_fix
Revises: 0017_ios_month_input_fix
Create Date: 2026-09-25
"""

from alembic import op

revision = "0018_ios_date_time_input_fix"
down_revision = "0017_ios_month_input_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO app_releases (public_id,version,version_major,version_minor,version_patch,"
        "change_type,title,content_markdown,status,created_by,published_by,published_at,created_at,updated_at) "
        "VALUES ('ND7NFV4YG40M3J9BBJF9BF6D8V','1.2.3',1,2,3,'fix',"
        "'iPhoneの日付・時刻入力表示を修正',"
        "'予定追加画面で、iPhoneの日付と到着時刻の入力欄がフォームからはみ出す問題を修正しました。',"
        "'published','system','system',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM user_release_dismissals WHERE release_id IN "
        "(SELECT id FROM app_releases WHERE version='1.2.3')"
    )
    op.execute("DELETE FROM app_releases WHERE version='1.2.3'")
