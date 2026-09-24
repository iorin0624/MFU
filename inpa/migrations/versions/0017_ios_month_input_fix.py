"""Publish the iOS month input layout fix.

Revision ID: 0017_ios_month_input_fix
Revises: 0016_mobile_layout_fix
Create Date: 2026-09-25
"""

from alembic import op

revision = "0017_ios_month_input_fix"
down_revision = "0016_mobile_layout_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO app_releases (public_id,version,version_major,version_minor,version_patch,"
        "change_type,title,content_markdown,status,created_by,published_by,published_at,created_at,updated_at) "
        "VALUES ('KNK973GPWDHRYYT9CNZX6JZ6VH','1.2.2',1,2,2,'fix',"
        "'iPhoneの月選択表示を修正',"
        "'iPhoneで統合カレンダーの月選択欄がフォームからはみ出す問題を修正しました。',"
        "'published','system','system',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM user_release_dismissals WHERE release_id IN "
        "(SELECT id FROM app_releases WHERE version='1.2.2')"
    )
    op.execute("DELETE FROM app_releases WHERE version='1.2.2'")
