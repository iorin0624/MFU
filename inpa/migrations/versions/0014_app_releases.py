"""Add application releases and per-user dismissal history.

Revision ID: 0014_app_releases
Revises: 0013_feedbacks
Create Date: 2026-09-25
"""

from alembic import op

revision = "0014_app_releases"
down_revision = "0013_feedbacks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE app_releases ("
        "id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,"
        "public_id CHAR(26) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,"
        "version VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,"
        "version_major INT UNSIGNED NOT NULL,version_minor INT UNSIGNED NOT NULL,"
        "version_patch INT UNSIGNED NOT NULL,change_type VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,"
        "title VARCHAR(120) NOT NULL,content_markdown TEXT NOT NULL,"
        "status VARCHAR(16) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT 'draft',"
        "created_by VARCHAR(128) NOT NULL,published_by VARCHAR(128) NULL,published_at DATETIME(6) NULL,"
        "created_at DATETIME(6) NOT NULL,updated_at DATETIME(6) NOT NULL,"
        "PRIMARY KEY (id),UNIQUE KEY uq_app_releases_public_id (public_id),"
        "UNIQUE KEY uq_app_releases_version (version),"
        "UNIQUE KEY uq_app_releases_parts (version_major,version_minor,version_patch),"
        "KEY ix_app_releases_status_version (status,version_major,version_minor,version_patch),"
        "CONSTRAINT ck_app_releases_type CHECK (change_type IN ('major','feature','fix')),"
        "CONSTRAINT ck_app_releases_status CHECK (status IN ('draft','published'))"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )
    op.execute(
        "CREATE TABLE user_release_dismissals ("
        "user_id BIGINT UNSIGNED NOT NULL,release_id BIGINT UNSIGNED NOT NULL,dismissed_at DATETIME(6) NOT NULL,"
        "PRIMARY KEY (user_id,release_id),KEY ix_release_dismissals_release (release_id),"
        "CONSTRAINT fk_release_dismissals_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,"
        "CONSTRAINT fk_release_dismissals_release FOREIGN KEY (release_id) REFERENCES app_releases(id) ON DELETE CASCADE"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )
    op.execute(
        "INSERT INTO app_releases (public_id,version,version_major,version_minor,version_patch,change_type,"
        "title,content_markdown,status,created_by,published_by,published_at,created_at,updated_at) VALUES "
        "('7BXHCS8E1XHT5688NMTCPGX8V4','1.0.0',1,0,0,'major','INPA 初回公開',"
        "'プロフィール、シーズン別カレンダー、予定管理、公開範囲、つながり、共有URL、管理機能を公開しました。',"
        "'published','system','system','2026-09-21 00:00:00','2026-09-21 00:00:00','2026-09-21 00:00:00'),"
        "('CMAN5BASPQ44PNX105TC7Y6J03','1.1.0',1,1,0,'feature','アップデート情報を追加',"
        "'アップデート履歴、現在のバージョン表示、新着アップデートのお知らせ機能を追加しました。',"
        "'published','system','system',UTC_TIMESTAMP(6),UTC_TIMESTAMP(6),UTC_TIMESTAMP(6))"
    )


def downgrade() -> None:
    op.execute("DROP TABLE user_release_dismissals")
    op.execute("DROP TABLE app_releases")
