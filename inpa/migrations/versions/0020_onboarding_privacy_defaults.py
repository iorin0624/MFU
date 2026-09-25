"""Publish recommended onboarding privacy defaults.

Revision ID: 0020_onboarding_privacy_defaults
Revises: 0019_onboarding_wizard
Create Date: 2026-09-25
"""

from alembic import op

revision = "0020_onboarding_privacy_defaults"
down_revision = "0019_onboarding_wizard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO app_releases (public_id,version,version_major,version_minor,version_patch,"
        "change_type,title,content_markdown,status,created_by,published_by,published_at,created_at,updated_at) "
        "VALUES ('WMW2YKVP76R9BD5ZKEX8KV9Q5G','1.3.1',1,3,1,'fix',"
        "'初回公開設定におすすめを適用',"
        "'初回設定ウィザードの公開範囲に、おすすめのチェック状態をあらかじめ設定するようにしました。',"
        "'published','system','system',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM user_release_dismissals WHERE release_id IN "
        "(SELECT id FROM app_releases WHERE version='1.3.1')"
    )
    op.execute("DELETE FROM app_releases WHERE version='1.3.1'")
