ALTER TABLE uploads
    ADD COLUMN upload_deleted_at DATETIME NULL AFTER created_at,
    ADD COLUMN layer_deleted_at DATETIME NULL AFTER upload_deleted_at;

CREATE INDEX idx_uploads_upload_deleted_at ON uploads (upload_deleted_at);
CREATE INDEX idx_uploads_layer_deleted_at ON uploads (layer_deleted_at);
