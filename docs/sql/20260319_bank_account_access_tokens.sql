CREATE TABLE IF NOT EXISTS mfu_payout_access_token (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    token_hash VARCHAR(255) NOT NULL,
    token_prefix VARCHAR(32) NOT NULL,
    token_suffix VARCHAR(32) NOT NULL,
    memo VARCHAR(255) NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    access_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    last_accessed_at DATETIME NULL,
    last_access_ip VARCHAR(64) NULL,
    issued_via VARCHAR(32) NOT NULL DEFAULT 'admin_ui',
    issued_by_app VARCHAR(64) NULL,
    created_by_admin VARCHAR(64) NULL,
    expires_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    INDEX idx_mfu_payout_access_token_active (is_active),
    INDEX idx_mfu_payout_access_token_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS mfu_payout_token_api_client (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    app_name VARCHAR(64) NOT NULL,
    api_key_hash VARCHAR(255) NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    last_used_at DATETIME NULL,
    last_used_ip VARCHAR(64) NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uq_mfu_payout_token_api_client_app_name (app_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- seed example:
-- INSERT INTO mfu_payout_token_api_client (
--     app_name, api_key_hash, is_active, created_at, updated_at
-- ) VALUES (
--     'album', SHA2('replace-with-strong-raw-api-key', 256), 1, NOW(), NOW()
-- );
