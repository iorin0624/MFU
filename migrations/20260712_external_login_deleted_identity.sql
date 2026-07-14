CREATE TABLE IF NOT EXISTS external_login_deleted_identity (
    provider VARCHAR(32) NOT NULL,
    identity_hash CHAR(64) NOT NULL,
    original_user_id BIGINT UNSIGNED NULL,
    deleted_at DATETIME NOT NULL,
    deleted_by VARCHAR(80) NULL,
    deletion_reason VARCHAR(255) NULL,
    PRIMARY KEY (provider, identity_hash),
    INDEX idx_ext_deleted_identity_user (original_user_id),
    INDEX idx_ext_deleted_identity_deleted_at (deleted_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
