"""Add resumable onboarding and publish the onboarding wizard.

Revision ID: 0019_onboarding_wizard
Revises: 0018_ios_date_time_input_fix
Create Date: 2026-09-25
"""

from alembic import op

revision = "0019_onboarding_wizard"
down_revision = "0018_ios_date_time_input_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN onboarding_step VARCHAR(16) CHARACTER SET ascii "
        "COLLATE ascii_bin NOT NULL DEFAULT 'completed'"
    )
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT ck_users_onboarding_step "
        "CHECK (onboarding_step IN ('privacy','visit','completed'))"
    )
    op.execute(
        "INSERT INTO app_releases (public_id,version,version_major,version_minor,version_patch,"
        "change_type,title,content_markdown,status,created_by,published_by,published_at,created_at,updated_at) "
        "VALUES ('M0MPYS3E2CHT3J1CFG9ZYABF09','1.3.0',1,3,0,'feature',"
        "'初回設定ウィザードを追加',"
        "'新規登録時に、プロフィール作成、公開範囲設定、最初の予定追加を順番に案内するウィザードを追加しました。途中で閉じても次回ログイン時に続きから再開できます。',"
        "'published','system','system',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM user_release_dismissals WHERE release_id IN "
        "(SELECT id FROM app_releases WHERE version='1.3.0')"
    )
    op.execute("DELETE FROM app_releases WHERE version='1.3.0'")
    op.execute("ALTER TABLE users DROP CONSTRAINT ck_users_onboarding_step")
    op.execute("ALTER TABLE users DROP COLUMN onboarding_step")
