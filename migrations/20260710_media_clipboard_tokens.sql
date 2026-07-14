CREATE TABLE IF NOT EXISTS media_clipboard_tokens (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  token_hash CHAR(64) NOT NULL,
  username VARCHAR(191) NOT NULL,
  label VARCHAR(120) NOT NULL DEFAULT 'MFU Media Clipboard',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  expires_at DATETIME NULL,
  last_used_at DATETIME NULL,
  revoked_at DATETIME NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_media_clipboard_tokens_hash (token_hash),
  KEY idx_media_clipboard_tokens_username (username),
  KEY idx_media_clipboard_tokens_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
