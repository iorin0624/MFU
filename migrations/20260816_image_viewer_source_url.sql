ALTER TABLE image_viewer_files
    ADD COLUMN IF NOT EXISTS source_url TEXT NULL AFTER checksum_sha256;
