-- up
ALTER TABLE chat_messages
  ADD COLUMN reply_to_message_id BIGINT NULL AFTER body,
  ADD KEY idx_chat_messages_reply_to (reply_to_message_id);

ALTER TABLE chat_messages
  ADD CONSTRAINT fk_chat_messages_reply_to
  FOREIGN KEY (reply_to_message_id) REFERENCES chat_messages(id)
  ON DELETE SET NULL;

-- down
ALTER TABLE chat_messages DROP FOREIGN KEY fk_chat_messages_reply_to;
ALTER TABLE chat_messages DROP KEY idx_chat_messages_reply_to;
ALTER TABLE chat_messages DROP COLUMN reply_to_message_id;
