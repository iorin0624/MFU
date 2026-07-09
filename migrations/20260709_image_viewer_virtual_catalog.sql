CREATE TABLE IF NOT EXISTS image_viewer_folders (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    folder_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    parent_id BIGINT UNSIGNED NULL,
    folder_name VARCHAR(255) NOT NULL,
    status ENUM('active', 'trash') NOT NULL DEFAULT 'active',
    trashed_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_image_viewer_folder_uuid (folder_uuid),
    UNIQUE KEY uq_image_viewer_folder_name (parent_id, folder_name),
    KEY idx_image_viewer_folder_parent (parent_id),
    CONSTRAINT fk_image_viewer_folder_parent
        FOREIGN KEY (parent_id) REFERENCES image_viewer_folders (id)
        ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT INTO image_viewer_folders
    (id, folder_uuid, parent_id, folder_name, status)
VALUES
    (1, '00000000-0000-0000-0000-000000000000', NULL, '', 'active')
ON DUPLICATE KEY UPDATE folder_name = VALUES(folder_name), status = 'active';

CREATE TABLE IF NOT EXISTS image_viewer_files (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    file_uuid CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    folder_id BIGINT UNSIGNED NOT NULL,
    display_name VARCHAR(255) NOT NULL,
    storage_relpath VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    thumbnail_relpath VARCHAR(255) CHARACTER SET ascii COLLATE ascii_bin NULL,
    extension VARCHAR(16) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL,
    media_type ENUM('image', 'video') NOT NULL,
    mime_type VARCHAR(127) NULL,
    file_size BIGINT UNSIGNED NOT NULL,
    file_mtime DATETIME(6) NOT NULL,
    checksum_sha256 BINARY(32) NULL,
    width INT UNSIGNED NULL,
    height INT UNSIGNED NULL,
    status ENUM('processing', 'active', 'trash', 'missing') NOT NULL
        DEFAULT 'processing',
    trashed_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
        ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_image_viewer_file_uuid (file_uuid),
    UNIQUE KEY uq_image_viewer_storage_relpath (storage_relpath),
    UNIQUE KEY uq_image_viewer_folder_filename (folder_id, display_name),
    KEY idx_image_viewer_folder_updated (folder_id, updated_at),
    KEY idx_image_viewer_status (status),
    KEY idx_image_viewer_checksum (checksum_sha256),
    CONSTRAINT fk_image_viewer_file_folder
        FOREIGN KEY (folder_id) REFERENCES image_viewer_folders (id)
        ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
