"""Publish registration link validation.

Revision ID: 0021_registration_link_validation
Revises: 0020_onboarding_privacy_defaults
Create Date: 2026-09-25
"""

from alembic import op

revision = "0021_registration_link_validation"
down_revision = "0020_onboarding_privacy_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO app_releases (public_id,version,version_major,version_minor,version_patch,"
        "change_type,title,content_markdown,status,created_by,published_by,published_at,created_at,updated_at) "
        "VALUES ('T6JQD9VN45KCP6715RE6G83EKG','1.3.2',1,3,2,'fix',"
        "'使用済み登録リンクを無効化',"
        "'メールアドレス確認後に使用した登録リンクを再度開いても、登録フォームを表示しないようにしました。',"
        "'published','system','system',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM user_release_dismissals WHERE release_id IN "
        "(SELECT id FROM app_releases WHERE version='1.3.2')"
    )
    op.execute("DELETE FROM app_releases WHERE version='1.3.2'")
