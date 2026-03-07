CREATE TABLE IF NOT EXISTS chat_active_views (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  actor_type VARCHAR(16) NOT NULL,
  actor_id VARCHAR(64) NOT NULL,
  event_id BIGINT UNSIGNED NOT NULL,
  room_id VARCHAR(64) NOT NULL,
  client_id VARCHAR(64) NOT NULL,
  is_visible TINYINT(1) NOT NULL DEFAULT 1,
  entered_at DATETIME NOT NULL,
  last_ping_at DATETIME NOT NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uq_chat_active_view_actor_client (actor_type, actor_id, client_id),
  KEY idx_chat_active_view_room (room_id),
  KEY idx_chat_active_view_lookup (actor_type, actor_id, room_id, is_visible, last_ping_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
