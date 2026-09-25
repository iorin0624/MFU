"""Add password-change auditing and WebAuthn passkeys.

Revision ID: 0022_security_credentials
Revises: 0021_registration_link_check
Create Date: 2026-09-25
"""

from alembic import op

revision = "0022_security_credentials"
down_revision = "0021_registration_link_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE IF NOT EXISTS user_passkeys ("
        "id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
        "public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,"
        "user_id BIGINT UNSIGNED NOT NULL,"
        "credential_id VARBINARY(1024) NOT NULL,credential_public_key BLOB NOT NULL,"
        "name VARCHAR(80) NOT NULL,sign_count BIGINT UNSIGNED NOT NULL DEFAULT 0,"
        "transports VARCHAR(255) NULL,device_type VARCHAR(32) NULL,backed_up TINYINT(1) NOT NULL DEFAULT 0,"
        "created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),last_used_at DATETIME(6) NULL,"
        "updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),"
        "revoked_at DATETIME(6) NULL,PRIMARY KEY (id),UNIQUE KEY uq_passkeys_public_id (public_id),"
        "UNIQUE KEY uq_passkeys_credential (credential_id),KEY ix_passkeys_user_active (user_id,revoked_at),"
        "CONSTRAINT fk_passkeys_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,"
        "CONSTRAINT chk_passkeys_backed_up CHECK (backed_up IN (0,1))) ENGINE=InnoDB"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS webauthn_challenges ("
        "id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
        "public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,"
        "user_id BIGINT UNSIGNED NULL,purpose VARCHAR(24) NOT NULL,challenge VARBINARY(64) NOT NULL,"
        "created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),expires_at DATETIME(6) NOT NULL,"
        "used_at DATETIME(6) NULL,PRIMARY KEY (id),UNIQUE KEY uq_webauthn_challenge_public (public_id),"
        "UNIQUE KEY uq_webauthn_challenge_value (challenge),"
        "KEY ix_webauthn_challenge_expiry (expires_at,used_at),"
        "CONSTRAINT fk_webauthn_challenge_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,"
        "CONSTRAINT chk_webauthn_challenge_purpose CHECK "
        "(purpose IN ('registration','authentication'))) ENGINE=InnoDB"
    )
    op.execute(
        "INSERT INTO app_releases (public_id,version,version_major,version_minor,version_patch,"
        "change_type,title,content_markdown,status,created_by,published_by,published_at,created_at,updated_at) "
        "VALUES ('M7R6XPQ2G5A9K4V8D3N1T0JHCS','1.4.0',1,4,0,'feature','パスワード変更とパスキーに対応',"
        "'プロフィールからパスワードを変更できるようにし、Face ID・Touch ID・Windows Hello等でログインできるパスキーを追加しました。',"
        "'published','system','system',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6)) "
        "ON DUPLICATE KEY UPDATE title=VALUES(title),content_markdown=VALUES(content_markdown),"
        "status=VALUES(status),updated_at=UTC_TIMESTAMP(6)"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM user_release_dismissals WHERE release_id IN (SELECT id FROM app_releases WHERE version='1.4.0')"
    )
    op.execute("DELETE FROM app_releases WHERE version='1.4.0'")
    op.execute("DROP TABLE webauthn_challenges")
    op.execute("DROP TABLE user_passkeys")
