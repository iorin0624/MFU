CREATE TABLE IF NOT EXISTS chat_dm_conversations (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid CHAR(36) NOT NULL,
  dm_type VARCHAR(32) NOT NULL,
  pair_key VARCHAR(255) NOT NULL,
  last_message_id BIGINT UNSIGNED NULL,
  last_message_at DATETIME NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_chat_dm_conversations_uuid (uuid),
  UNIQUE KEY uq_chat_dm_conversations_pair_key (pair_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_dm_participants (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  conversation_id BIGINT UNSIGNED NOT NULL,
  actor_key VARCHAR(128) NOT NULL,
  display_name_cache VARCHAR(255) NULL,
  last_read_message_id BIGINT UNSIGNED NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_chat_dm_participants (conversation_id, actor_key),
  KEY idx_chat_dm_participants_actor_key (actor_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS chat_dm_messages (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  conversation_id BIGINT UNSIGNED NOT NULL,
  sender_actor_key VARCHAR(128) NOT NULL,
  body_type VARCHAR(32) NOT NULL DEFAULT 'text',
  body_text TEXT NULL,
  body_json JSON NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_chat_dm_messages_conv_id (conversation_id, id),
  KEY idx_chat_dm_messages_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

REPLACE INTO settings (`key`, `value`) VALUES ('CHAT_DM_ENABLE_USER_USER', '0');
