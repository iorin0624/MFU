ALTER TABLE uploader_tokens
    ADD COLUMN scope VARCHAR(32) NOT NULL DEFAULT 'desktop_upload' AFTER label;

CREATE INDEX idx_uploader_tokens_scope
    ON uploader_tokens (username, scope, revoked_at);
