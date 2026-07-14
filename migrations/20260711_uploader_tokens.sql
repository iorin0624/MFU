CREATE TABLE IF NOT EXISTS uploader_tokens (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(255) NOT NULL,
    token_hash CHAR(64) NOT NULL UNIQUE,
    label VARCHAR(255) NOT NULL DEFAULT 'MFU Uploader',
    created_at DATETIME NOT NULL,
    last_used_at DATETIME NULL,
    expires_at DATETIME NULL,
    revoked_at DATETIME NULL,
    INDEX idx_uploader_tokens_username (username),
    INDEX idx_uploader_tokens_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
