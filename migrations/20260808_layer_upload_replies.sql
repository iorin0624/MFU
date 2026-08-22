CREATE TABLE IF NOT EXISTS layer_upload_replies (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    upload_id INT NOT NULL,
    reply_uuid CHAR(32) NOT NULL,
    title_snapshot TEXT NOT NULL,
    comment TEXT NULL,
    posted_at DATETIME(6) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uniq_layer_reply_uuid (reply_uuid),
    KEY idx_layer_reply_upload_posted (upload_id, posted_at, id),
    CONSTRAINT fk_layer_reply_upload
        FOREIGN KEY (upload_id) REFERENCES uploads (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS layer_upload_reply_files (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    reply_id BIGINT UNSIGNED NOT NULL,
    file_kind ENUM('image', 'zip') NOT NULL,
    filename VARCHAR(512) NOT NULL,
    sort_order INT UNSIGNED NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uniq_layer_reply_file (reply_id, file_kind, filename),
    KEY idx_layer_reply_file_order (reply_id, file_kind, sort_order, id),
    CONSTRAINT fk_layer_reply_file_reply
        FOREIGN KEY (reply_id) REFERENCES layer_upload_replies (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
