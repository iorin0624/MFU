CREATE TABLE IF NOT EXISTS phone_whitelist_entries (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    phone_number VARCHAR(16) NOT NULL UNIQUE,
    name VARCHAR(100) NOT NULL DEFAULT '',
    note VARCHAR(500) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_phone_whitelist_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS phone_whitelist_sync_state (
    id TINYINT NOT NULL PRIMARY KEY,
    last_synced_at DATETIME NULL,
    entry_count INT NOT NULL DEFAULT 0,
    updated_by VARCHAR(128) NOT NULL DEFAULT '',
    last_result VARCHAR(32) NOT NULL DEFAULT '',
    message VARCHAR(500) NOT NULL DEFAULT '',
    whitelist_disabled_until DATETIME NULL,
    anonymous_allowed_until DATETIME NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

ALTER TABLE phone_whitelist_sync_state
    ADD COLUMN IF NOT EXISTS whitelist_disabled_until DATETIME NULL,
    ADD COLUMN IF NOT EXISTS anonymous_allowed_until DATETIME NULL;
