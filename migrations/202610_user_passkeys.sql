CREATE TABLE IF NOT EXISTS user_passkeys (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(64) NOT NULL,
  label VARCHAR(128) NULL,
  credential_id VARCHAR(512) NOT NULL UNIQUE,
  public_key TEXT NOT NULL,
  sign_count INT NOT NULL DEFAULT 0,
  transports TEXT NULL,
  aaguid VARCHAR(36) NULL,
  uv TINYINT NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at DATETIME NULL,
  INDEX idx_user_passkeys_username (username)
);
