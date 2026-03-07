-- One-time data correction for DM notifications created before actor-key normalization fix.
-- Fixes admin DM rows saved with recipient_key='1' so MFU unread-count can match username='admin'.

UPDATE mfu_notifications
SET recipient_key = 'admin'
WHERE user_kind = 'mfu'
  AND kind = 'dm'
  AND recipient_key = '1';
