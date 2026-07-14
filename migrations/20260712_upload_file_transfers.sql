CREATE TABLE IF NOT EXISTS upload_file_transfers (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    upload_id BIGINT NOT NULL,
    client_file_id VARCHAR(64) NOT NULL,
    original_filename VARCHAR(255) NOT NULL,
    saved_filename VARCHAR(255) NULL,
    expected_sha256 CHAR(64) NOT NULL,
    actual_sha256 CHAR(64) NULL,
    file_size BIGINT UNSIGNED NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    error_message VARCHAR(1024) NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE KEY uq_upload_file_transfer (upload_id, client_file_id),
    INDEX ix_upload_file_transfer_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
