CREATE TABLE IF NOT EXISTS mfu_notifications (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  user_kind VARCHAR(16) NOT NULL,
  user_id BIGINT UNSIGNED NOT NULL,
  kind VARCHAR(64) NOT NULL,
  title VARCHAR(255) NOT NULL,
  body TEXT NULL,
  target_url VARCHAR(512) NOT NULL,
  event_id BIGINT UNSIGNED NULL,
  created_at DATETIME NOT NULL,
  read_at DATETIME NULL,
  dedup_key VARCHAR(191) NOT NULL,
  PRIMARY KEY (id),
  UNIQUE KEY uq_mfu_notifications_dedup (user_kind, user_id, dedup_key),
  KEY idx_mfu_notifications_unread (user_kind, user_id, read_at, created_at),
  KEY idx_mfu_notifications_created (user_kind, user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
