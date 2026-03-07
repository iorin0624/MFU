-- MFU通知(admin/ACL)向けに recipient_key(username) と表示補助カラムを追加
ALTER TABLE mfu_notifications
  ADD COLUMN recipient_key VARCHAR(191) NULL AFTER user_id,
  ADD COLUMN room_type VARCHAR(32) NULL AFTER chat_room_id,
  ADD COLUMN room_id VARCHAR(64) NULL AFTER room_type,
  ADD COLUMN sender_label VARCHAR(255) NULL AFTER room_id;

ALTER TABLE mfu_notifications
  ADD KEY idx_mfu_notifications_recipient_unread (user_kind, recipient_key, read_at, created_at),
  ADD KEY idx_mfu_notifications_recipient_created (user_kind, recipient_key, created_at);
