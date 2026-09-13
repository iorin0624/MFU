ALTER TABLE image_viewer_folders
    ADD COLUMN IF NOT EXISTS numbering_enabled TINYINT(1) NOT NULL DEFAULT 1 AFTER folder_name,
    ADD COLUMN IF NOT EXISTS numbering_digits TINYINT UNSIGNED NOT NULL DEFAULT 1 AFTER numbering_enabled;
