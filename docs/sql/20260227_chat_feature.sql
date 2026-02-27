CREATE TABLE IF NOT EXISTS chat_messages (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  event_id BIGINT NOT NULL,
  sender_actor_type VARCHAR(16) NOT NULL,
  sender_actor_id VARCHAR(64) NOT NULL,
  sender_display_name VARCHAR(255) NOT NULL,
  body TEXT NOT NULL,
  created_at DATETIME NOT NULL,
  INDEX idx_chat_messages_event_created (event_id, created_at),
  INDEX idx_chat_messages_sender (event_id, sender_actor_type, sender_actor_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_push_subscriptions (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  actor_type VARCHAR(16) NOT NULL,
  actor_id VARCHAR(64) NOT NULL,
  endpoint TEXT NOT NULL,
  p256dh VARCHAR(255) NOT NULL,
  auth VARCHAR(255) NOT NULL,
  user_agent VARCHAR(512) NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  UNIQUE KEY uq_actor_endpoint (actor_type, actor_id, endpoint(255))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS chat_notification_log (
  id BIGINT PRIMARY KEY AUTO_INCREMENT,
  event_id BIGINT NOT NULL,
  kind VARCHAR(64) NOT NULL,
  payload_json JSON NULL,
  sent_count INT NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL,
  INDEX idx_chat_notification_event_created (event_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
